/* Plate-mode label/time formatters, grid placement, layout, site elements and
   view-menu positioning. Pure helpers plus a dependency-injected mode factory;
   no DOM side effects at module evaluation time. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Nd2PlateMode = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function siteLabel(name) {
    // "10(5)_MOI" -> {dil: "10", exp: "5", cond: "MOI"}; other names verbatim
    const m = /^(\d+)\((\d+)\)_(.+)$/.exec(String(name || ""));
    return m ? { dil: m[1], exp: m[2], cond: m[3] } : { dil: String(name || ""), exp: "", cond: "" };
  }

  function superscript(digits) {
    return String(digits).replace(/\d/g, (c) => "⁰¹²³⁴⁵⁶⁷⁸⁹"[Number(c)]);
  }

  function siteLabelText(name) {
    const l = siteLabel(name);
    return l.exp ? l.dil + superscript(l.exp) + " " + l.cond : l.dil;
  }

  function fillSitePill(pill, name, document) {
    pill.replaceChildren();
    const l = siteLabel(name);
    const dil = document.createElement("span");
    dil.className = "dil";
    dil.textContent = l.dil;
    if (l.exp) {
      const sup = document.createElement("sup");
      sup.textContent = l.exp;
      dil.append(sup);
    }
    pill.append(dil);
    if (l.cond) {
      const sep = document.createElement("span");
      sep.className = "sep";
      sep.textContent = "·";
      const cond = document.createElement("span");
      cond.className = "cond";
      cond.textContent = l.cond;
      pill.append(sep, cond);
    }
  }

  function fmtClock(ms) {
    // "0 H 30 M": whole minutes, hours first, as the time badge reads
    const total = Math.max(0, Math.round(Number(ms || 0) / 60000));
    const h = Math.floor(total / 60);
    const m = total % 60;
    return h + " H " + String(m).padStart(2, "0") + " M";
  }

  function fmtPeriod(ms) {
    const total = Math.max(0, Math.round(Number(ms || 0) / 60000));
    if (total < 60) return total + " M";
    return fmtClock(ms);
  }

  function fmtTickLabel(min) {
    const h = Math.floor(min / 60);
    const m = Math.round(min - h * 60);
    if (h === 0) return m + " m";
    return m ? h + " h " + m + " m" : h + " h";
  }

  function fmtHm(ms) {
    // "6:30": hours and minutes, the way a scrubber reads
    const total = Math.max(0, Math.round(Number(ms || 0) / 60000));
    const h = Math.floor(total / 60);
    const m = total % 60;
    return h + ":" + String(m).padStart(2, "0");
  }

  function fmtPeriodWords(ms) {
    const total = Math.max(0, Math.round(Number(ms || 0) / 60000));
    if (total < 60) return total + " min";
    const h = Math.floor(total / 60);
    const m = total % 60;
    return m ? h + " h " + m + " min" : h + " h";
  }

  function dilNode(l, document) {
    const span = document.createElement("span");
    span.textContent = l.dil;
    if (l.exp) {
      const sup = document.createElement("sup");
      sup.textContent = l.exp;
      span.append(sup);
    }
    return span;
  }

  function createMode({
    state, $, document, window: win, localStorage: storage = null, ResizeObserver: RO = null,
    rawValueLabel, activeFrameParams, plateZText, renderParams, plateGroupKey,
    plateArrange, wirePlateViewMenu, applyPlateView,
    buildZSlider, buildTimeLine, renderZSlider, renderTimeLine, renderPlateAuto, setPlateAuto,
    wirePlateWheel, wirePlateKeys, paintPlate, setPlateFocus, updateReadout, showToast,
  }) {
    function platePeriodMs() {
      const pl = state.info.plate;
      if (Number(pl.periodMs) > 0) return Number(pl.periodMs);
      const times = pl.timesMs || [];
      if (times.length < 2) return null;
      const diffs = [];
      for (let i = 1; i < times.length; i++) diffs.push(times[i] - times[i - 1]);
      diffs.sort((a, b) => a - b);
      const mid = diffs[Math.floor(diffs.length / 2)];
      return mid > 0 ? mid : null;
    }

    function plateInspectionRows(plate) {
      const placed = (plate.sites || []).slice()
        .sort((a, b) => a.row - b.row || a.col - b.col);
      const names = placed.map((s) => siteLabelText(s.name)).join(", ");
      const rows = [
        ["Sites", plate.P + " · stage arrangement " + plate.cols + " × " + plate.rows +
          (names ? " · " + names : "")],
        ["Z", plate.Z + " planes" +
          (plate.zStepUm ? " · d = " + rawValueLabel(Number(plate.zStepUm)) + " µm" : "") +
          " · home " + (Number(plate.zHome) + 1)],
      ];
      const times = plate.timesMs || [];
      const period = platePeriodMs();
      rows.push(["Time", plate.T + " frames" +
        (times.length ? " · " + fmtClock(times[times.length - 1]) : " · timing unavailable") +
        (period ? " · every " + fmtPeriod(period) : "")]);
      if (plate.exposureMs !== null && plate.exposureMs !== undefined) {
        rows.push(["Exposure", rawValueLabel(Number(plate.exposureMs)) + " ms"]);
      }
      return rows;
    }

    function currentFocusSummary() {
      const pl = state.plate;
      const sites = pl.focus === null ? state.info.plate.sites.map((s) => s.i) : [pl.focus];
      return win.Nd2PlateUI.focusSummary(pl.focusMap, pl.t, sites, pl.z, state.info.plate.Z, pl.auto);
    }

    function platePlaneNote() {
      const pl = state.plate;
      const info = state.info.plate;
      const bits = [];
      if (pl.focus !== null && info.sites[pl.focus]) bits.push(siteLabelText(info.sites[pl.focus].name));
      bits.push("t " + (pl.t + 1) + "/" + info.T);
      bits.push(plateZText());
      return bits.join(" · ");
    }

    function platePlaneLabel() {
      const f = activeFrameParams();
      return f ? "T" + f.t + " · P" + f.p + " · Z" + f.z : platePlaneNote();
    }

    function plateFrameUrl(t, p, z, k) {
      const q = renderParams(new URLSearchParams({ k: k || state.plate.k }));
      return "api/plate/frame/" + t + "/" + p + "/" + z + ".jpg?" + q.toString();
    }

    function plateCellZoom() {
      // CSS px per image px of one grid cell; null before the first layout
      const pl = state.plate;
      return pl && pl.cellW ? pl.cellW / state.info.plate.frameW : null;
    }

    function placePlate() {
      const pl = state.plate;
      const block = $("plate-block");
      block.style.setProperty("--cols", pl.cols);
      block.style.setProperty("--rows", pl.rows);
      const grid = $("plate-grid");
      grid.setAttribute("aria-rowcount", String(pl.rows));
      grid.setAttribute("aria-colcount", String(pl.cols));
      const rows = Array.from({ length: pl.rows }, (_, i) => {
        const row = document.createElement("div");
        row.className = "plate-accessible-row";
        row.setAttribute("role", "row");
        row.setAttribute("aria-rowindex", String(i + 1));
        return row;
      });
      if (!pl.placed.some((s) => s.i === pl.gridActive)) pl.gridActive = pl.placed[0]?.i;
      pl.placed.forEach((s) => {
        const el = pl.gridEls[s.i];
        el.style.gridRow = s.row + 1;
        el.style.gridColumn = s.col + 1;
        el.setAttribute("role", "gridcell");
        el.setAttribute("aria-rowindex", String(s.row + 1));
        el.setAttribute("aria-colindex", String(s.col + 1));
        el.tabIndex = s.i === pl.gridActive ? 0 : -1;
        rows[s.row].append(el);
      });
      grid.replaceChildren(...rows);
      const rowHeads = $("plate-row-heads");
      const colHeads = $("plate-col-heads");
      rowHeads.replaceChildren();
      colHeads.replaceChildren();
      block.classList.toggle("headed", pl.structured);
      if (pl.headerKind === "well") {
        pl.wellHeaders.rows.forEach((label) => {
          const head = document.createElement("span");
          head.textContent = label;
          rowHeads.append(head);
        });
        pl.wellHeaders.cols.forEach((label) => {
          const head = document.createElement("span");
          head.textContent = label;
          colHeads.append(head);
        });
      } else if (pl.headerKind === "assay") {
        for (let r = 0; r < pl.rows; r++) {
          const s = pl.placed.find((x) => x.row === r);
          const l = siteLabel(s ? s.name : "");
          const head = document.createElement("span");
          if (pl.transposed) head.textContent = l.cond; else head.append(dilNode(l, document));
          rowHeads.append(head);
        }
        for (let c = 0; c < pl.cols; c++) {
          const s = pl.placed.find((x) => x.col === c);
          const l = siteLabel(s ? s.name : "");
          const head = document.createElement("span");
          if (pl.transposed) head.append(dilNode(l, document)); else head.textContent = l.cond;
          colHeads.append(head);
        }
      }
      const btn = $("plate-transpose");
      btn.classList.toggle("on", pl.transposed);
      btn.setAttribute("aria-pressed", String(pl.transposed));
    }

    function positionPlateViewMenu() {
      const btn = $("tb-plate-view");
      const menu = $("plate-view-menu");
      if (!btn || !menu || menu.hidden) return;
      const r = btn.getBoundingClientRect();
      const width = menu.offsetWidth || 184;
      const height = menu.offsetHeight || 96;
      menu.style.left = Math.max(8, Math.min(win.innerWidth - width - 8, r.right - width)) + "px";
      menu.style.top = Math.max(8, Math.min(win.innerHeight - height - 8, r.bottom + 6)) + "px";
    }

    function setPlateViewMenuOpen(open) {
      const btn = $("tb-plate-view");
      const menu = $("plate-view-menu");
      if (!btn || !menu) return;
      const shown = !!open && !btn.hidden;
      menu.hidden = !shown;
      btn.classList.toggle("active", shown);
      btn.setAttribute("aria-expanded", String(shown));
      if (shown) {
        positionPlateViewMenu();
        const items = [...menu.querySelectorAll("[data-plate-view]")];
        items.forEach((item, i) => { item.tabIndex = i === 0 ? 0 : -1; });
        const first = items[0];
        if (first) first.focus({ preventScroll: true });
      }
    }

    function renderPlateViewMenu() {
      const pl = state.plate;
      if (!pl) return;
      $("plate-view-menu").querySelectorAll("[data-plate-view]").forEach((item) => {
        item.setAttribute("aria-checked", String(!!pl.view[item.dataset.plateView]));
      });
    }

    function markFrameLoaded(img) {
      // the cell drops its placeholder on the first frame; a time tick fills in
      // once every site's frame at that t and z has arrived
      const pl = state.plate;
      const cell = img.closest(".site");
      if (cell) cell.classList.remove("pending");
      const key = img.dataset.frame;
      const tz = img.dataset.group;
      if (!key || !tz) return;
      // the set counts sites, not frames: with autofocus a site whose plane
      // moved would otherwise count twice and fill the band on its own
      const seen = (pl.loaded.get(tz) || new Set());
      seen.add(key.split("/")[1]);
      pl.loaded.set(tz, seen);
      if (seen.size >= state.info.plate.P && tz.endsWith("/" + plateGroupKey())) renderTimeLine();
    }

    function makeSiteEl(site) {
      const el = document.createElement("button");
      el.type = "button";
      el.className = "site pending";
      el.dataset.p = site.i;
      el.title = siteLabelText(site.name);
      el.setAttribute("aria-label", siteLabelText(site.name));
      const img = document.createElement("img");
      img.alt = "";
      img.draggable = false;
      img.addEventListener("load", () => markFrameLoaded(img));
      img.addEventListener("error", () => {
        // a frame the server could not read would otherwise leave the cell
        // in its placeholder for good, with nothing said
        const cell = img.closest(".site");
        if (!cell || img.dataset.frame !== cell.dataset.shown) return;
        cell.classList.remove("pending");
        cell.classList.add("failed");
        showToast("A frame of " + siteLabelText(site.name) + " could not be read");
      });
      const pill = document.createElement("span");
      pill.className = "pill";
      fillSitePill(pill, site.name, document);
      el.append(img, pill);
      el.addEventListener("click", () => setPlateFocus(Number(el.dataset.p)));
      return el;
    }

    function layoutPlate() {
      const pl = state.plate;
      if (!pl) return;
      const info = state.info.plate;
      const stage = $("stage-wrap").getBoundingClientRect();
      const r = $("plate").getBoundingClientRect();
      if (!r.width || !r.height) return;
      const gap = 12;
      // The first column holds either headings or the 28 px transpose button;
      // the first row always holds that button. Account for the actual chrome so
      // a dense 16 x 16 grid never clips at the bottom.
      const leadingW = (pl.structured ? 44 : 28) + gap;
      const topChromeH = 28 + 10;
      const sliderW = pl.view.zAxis ? 44 + gap : 0;
      const aspect = info.frameW && info.frameH ? info.frameW / info.frameH : 1;
      const availW = r.width - leadingW - sliderW;
      const availH = r.height - topChromeH;
      const cellW = Math.floor(Math.min(
        (availW - gap * (pl.cols - 1)) / pl.cols,
        ((availH - gap * (pl.rows - 1)) / pl.rows) * aspect
      ));
      const cellH = Math.floor(cellW / aspect);
      if (cellW < 8 || cellH < 8) return;
      pl.cellW = cellW;
      pl.cellH = cellH;
      const block = $("plate-block");
      block.style.setProperty("--cell-w", cellW + "px");
      block.style.setProperty("--cell-h", cellH + "px");
      const gridW = cellW * pl.cols + gap * (pl.cols - 1);
      const gridH = cellH * pl.rows + gap * (pl.rows - 1);
      block.style.setProperty("--grid-h", gridH + "px");
      const capsule = Math.max(420, Math.min(stage.width - 32, Math.max(gridW + 140, 760)));
      $("plate-stage").style.setProperty("--capsule-w", capsule + "px");
      const k = cellW <= 260 ? 8 : 4;
      if (k !== pl.k) {
        pl.k = k;
        paintPlate();
      }
      renderZSlider();
      if (pl.focus === null) updateReadout();
    }

    function buildPlate(onLoadFocus) {
      const pl = state.plate;
      const info = state.info.plate;
      const wrap = $("stage-wrap");
      wrap.classList.add(pl.focus === null ? "plate-grid" : "plate-focus");
      wrap.classList.toggle("plate-single", info.P === 1);

      try {
        const saved = storage?.getItem("nd2wsi.plate.transposed");
        pl.transposed = saved == null ? true : saved === "1";
      } catch (_) { pl.transposed = true; }
      try {
        pl.auto = info.Z > 1 && storage?.getItem("nd2wsi.plate.autofocus") === "1";
      } catch (_) { pl.auto = false; }
      plateArrange();
      wirePlateViewMenu();
      applyPlateView(false);

      const grid = $("plate-grid");
      const strip = $("plate-strip");
      pl.gridEls = [];
      pl.stripEls = [];
      (info.sites || []).forEach((site) => {
        const cell = makeSiteEl(site);
        grid.append(cell);
        pl.gridEls[site.i] = cell;
        const small = makeSiteEl(site);
        strip.append(small);
        pl.stripEls[site.i] = small;
      });
      placePlate();
      grid.addEventListener("keydown", (ev) => {
        if (ev.metaKey || ev.ctrlKey || ev.altKey || ev.isComposing) return;
        const cell = ev.target.closest(".site");
        if (!cell) return;
        const next = win.Nd2PlateUI.nextGridSite(pl.placed, Number(cell.dataset.p), ev.key);
        if (next === null) return;
        ev.preventDefault();
        ev.stopPropagation();
        pl.gridActive = next;
        pl.gridEls.forEach((el, p) => { el.tabIndex = p === next ? 0 : -1; });
        pl.gridEls[next].focus({ preventScroll: true });
      });
      $("plate-back").onclick = () => setPlateFocus(null);
      $("plate-transpose").onclick = () => {
        pl.transposed = !pl.transposed;
        try { storage?.setItem("nd2wsi.plate.transposed", pl.transposed ? "1" : "0"); } catch (_) { /* private mode */ }
        plateArrange();
        placePlate();
        layoutPlate();
      };

      buildZSlider();
      buildTimeLine();
      wirePlateWheel();
      wirePlateKeys();
      $("t-auto").addEventListener("click", () => setPlateAuto(!state.plate.auto));
      renderPlateAuto();
      if (typeof onLoadFocus === "function") onLoadFocus();

      // #plate is inset in the stage, so its observer sees every window resize
      // too; the window listener is only the fallback, not a second layout pass.
      if (typeof RO === "function") {
        const plateObserver = new RO(() => layoutPlate());
        plateObserver.observe($("plate"));
        win.addEventListener("pagehide", () => plateObserver.disconnect(), { once: true });
      } else {
        win.addEventListener("resize", layoutPlate);
        win.addEventListener("pagehide", () => win.removeEventListener("resize", layoutPlate), { once: true });
      }
      layoutPlate();
      paintPlate();
      renderZSlider();
      renderTimeLine();
      $("plane-note").textContent = platePlaneNote();
    }

    return {
      siteLabel,
      superscript,
      siteLabelText,
      fillSitePill: (pill, name) => fillSitePill(pill, name, document),
      fmtClock,
      fmtPeriod,
      fmtTickLabel,
      platePeriodMs,
      plateInspectionRows,
      platePlaneLabel,
      currentFocusSummary,
      platePlaneNote,
      plateFrameUrl,
      plateCellZoom,
      fmtHm,
      fmtPeriodWords,
      dilNode: (l) => dilNode(l, document),
      placePlate,
      positionPlateViewMenu,
      setPlateViewMenuOpen,
      renderPlateViewMenu,
      buildPlate,
      makeSiteEl,
      markFrameLoaded,
      layoutPlate,
    };
  }

  return {
    siteLabel,
    superscript,
    siteLabelText,
    fillSitePill,
    fmtClock,
    fmtPeriod,
    fmtTickLabel,
    fmtHm,
    fmtPeriodWords,
    dilNode,
    createMode,
  };
});
