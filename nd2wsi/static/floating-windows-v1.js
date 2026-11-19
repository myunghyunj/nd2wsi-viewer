/* Floating panel geometry, persistence and pointer interactions. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Nd2FloatingWindows = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const clamp = (value, lo, hi) => Math.max(lo, Math.min(hi, value));

  function createManager({ stage, document, storage, fetch: fetchFn = null,
    uiSettingsUrl = "/api/ui-settings", setTimeout: setTimer = typeof setTimeout === "function" ? setTimeout : null,
    clearTimeout: clearTimer = typeof clearTimeout === "function" ? clearTimeout : null }) {
    const BASE_Z = 30;
    const zOrder = [];
    const windows = new Map();
    let pendingPanels = {};
    let syncTimer = null;

    function syncZIndices() {
      zOrder.forEach((panelEl, idx) => {
        panelEl.style.zIndex = BASE_Z + idx;
      });
    }

    function flushBackendPanels() {
      syncTimer = null;
      if (typeof fetchFn !== "function") {
        pendingPanels = {};
        return;
      }
      const keys = Object.keys(pendingPanels);
      if (!keys.length) return;
      const patch = { panels: { ...pendingPanels } };
      pendingPanels = {};
      Promise.resolve().then(() => fetchFn(uiSettingsUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      })).catch(() => { /* ignore 404s or offline unit tests */ });
    }

    function scheduleBackendSave(key, snapshot) {
      if (typeof fetchFn !== "function" || !key) return;
      pendingPanels[key] = snapshot;
      if (!setTimer) {
        flushBackendPanels();
        return;
      }
      if (syncTimer !== null && clearTimer) clearTimer(syncTimer);
      syncTimer = setTimer(flushBackendPanels, 150);
    }

    function createWindow(el, opts) {
      const body = el.querySelector(".win-body");
      const titlebar = el.querySelector(".win-titlebar");
      const store = "nd2wsi.win." + opts.key;
      let userEdited = false;

      let st = {
        rect: opts.def(),
        collapsed: false,
        hidden: !!opts.startClosed,
        zoomRestore: null,
      };
      try {
        // geometry and collapse persist; "closed" is session-only, so panels
        // come back in their opening state on the next launch
        const saved = JSON.parse(storage.getItem(store) || "null");
        if (saved && saved.rect && typeof saved.rect === "object") {
          const r = saved.rect;
          if (Number.isFinite(Number(r.x)) && Number.isFinite(Number(r.y)) && Number.isFinite(Number(r.w))) {
            st.rect = {
              x: Number(r.x),
              y: Number(r.y),
              w: Number(r.w),
              h: r.h == null ? null : (Number.isFinite(Number(r.h)) ? Number(r.h) : null),
            };
            st.collapsed = !!saved.collapsed;
          }
        }
      } catch (_) { /* private mode etc. */ }

      function persist() {
        userEdited = true;
        const snapshot = { rect: { ...st.rect }, collapsed: st.collapsed };
        try {
          storage.setItem(store, JSON.stringify(snapshot));
        } catch (_) { /* ignore */ }
        scheduleBackendSave(opts.key, snapshot);
      }

      function apply() {
        el.style.left = st.rect.x + "px";
        el.style.top = st.rect.y + "px";
        el.style.width = st.rect.w + "px";
        if (st.collapsed || st.rect.h == null) {
          el.style.height = "";
          el.style.maxHeight = Math.max(120, stage.clientHeight - st.rect.y - 12) + "px";
        } else {
          el.style.height = st.rect.h + "px";
          el.style.maxHeight = "";
        }
        el.classList.toggle("collapsed", st.collapsed);
        el.classList.toggle("hidden", st.hidden);
        if (opts.toolbarBtn) opts.toolbarBtn.classList.toggle("active", !st.hidden);
      }

      function clampToStage() {
        const sw = stage.clientWidth;
        const sh = stage.clientHeight;
        if (!(sw > 0 && sh > 0)) return;
        const maxAvailW = Math.max(opts.minW, Math.min(opts.maxW, sw - 20));
        st.rect.w = clamp(st.rect.w, opts.minW, maxAvailW);
        st.rect.x = clamp(st.rect.x, 6 - st.rect.w + 60, sw - 60);
        st.rect.y = clamp(st.rect.y, 6, Math.max(6, sh - 34));
        apply();
      }

      zOrder.push(el);
      syncZIndices();

      function focus() {
        const alreadyTop = zOrder[zOrder.length - 1] === el;
        document.querySelectorAll(".mac-window").forEach((w) => w.classList.remove("focused"));
        el.classList.add("focused");
        if (!alreadyTop) {
          const idx = zOrder.indexOf(el);
          if (idx !== -1) zOrder.splice(idx, 1);
          zOrder.push(el);
        }
        syncZIndices();
      }

      el.addEventListener("pointerdown", focus);

      // -- dragging by the title bar
      let dragFrom = null;
      titlebar.addEventListener("pointerdown", (ev) => {
        if ((ev.button != null && ev.button !== 0) || ev.target.closest(".tl")) return;
        dragFrom = { px: ev.clientX, py: ev.clientY, x: st.rect.x, y: st.rect.y };
        el.classList.add("dragging");
        if (opts.onMove) opts.onMove();
        titlebar.setPointerCapture(ev.pointerId);
        ev.preventDefault();
      });
      titlebar.addEventListener("pointermove", (ev) => {
        if (!dragFrom) return;
        st.rect.x = dragFrom.x + (ev.clientX - dragFrom.px);
        st.rect.y = dragFrom.y + (ev.clientY - dragFrom.py);
        clampToStage();
      });
      const finishDrag = () => {
        if (!dragFrom) return;
        dragFrom = null;
        el.classList.remove("dragging");
        persist();
      };
      titlebar.addEventListener("pointerup", finishDrag);
      titlebar.addEventListener("pointercancel", finishDrag);
      titlebar.addEventListener("lostpointercapture", finishDrag);
      titlebar.addEventListener("dblclick", (ev) => {
        if (ev.target.closest(".tl")) return;
        setCollapsed(!st.collapsed);
      });

      // -- resizing from edges and corners
      for (const dir of ["n", "s", "e", "w", "ne", "nw", "se", "sw"]) {
        const h = document.createElement("div");
        h.className = "rz rz-" + dir;
        el.append(h);
        let from = null;
        h.addEventListener("pointerdown", (ev) => {
          if (ev.button != null && ev.button !== 0) return;
          from = {
            px: ev.clientX,
            py: ev.clientY,
            x: st.rect.x,
            y: st.rect.y,
            w: st.rect.w,
            h: st.rect.h != null ? st.rect.h : el.offsetHeight,
          };
          el.classList.add("resizing");
          if (opts.onMove) opts.onMove();
          focus();
          h.setPointerCapture(ev.pointerId);
          ev.preventDefault();
          ev.stopPropagation();
        });
        h.addEventListener("pointermove", (ev) => {
          if (!from) return;
          const dx = ev.clientX - from.px;
          const dy = ev.clientY - from.py;
          const r = { ...st.rect, h: from.h };
          if (dir.includes("e")) r.w = from.w + dx;
          if (dir.includes("s")) r.h = from.h + dy;
          if (dir.includes("w")) { r.w = from.w - dx; r.x = from.x + dx; }
          if (dir.includes("n")) { r.h = from.h - dy; r.y = from.y + dy; }
          if (r.w < opts.minW) { if (dir.includes("w")) r.x -= opts.minW - r.w; r.w = opts.minW; }
          if (r.w > opts.maxW) { if (dir.includes("w")) r.x += r.w - opts.maxW; r.w = opts.maxW; }
          if (r.h < opts.minH) { if (dir.includes("n")) r.y -= opts.minH - r.h; r.h = opts.minH; }
          if (dir.includes("n") && r.y < 6) {
            r.h = Math.max(opts.minH, r.h - (6 - r.y));
            r.y = 6;
          }
          st.rect = r;
          apply();
          if (opts.onResize) opts.onResize();
        });
        const finishResize = () => {
          if (!from) return;
          from = null;
          el.classList.remove("resizing");
          persist();
        };
        h.addEventListener("pointerup", finishResize);
        h.addEventListener("pointercancel", finishResize);
        h.addEventListener("lostpointercapture", finishResize);
      }

      // -- traffic lights
      function setCollapsed(on) {
        st.collapsed = on;
        apply();
        persist();
        if (on && opts.onClose) opts.onClose();
      }
      function close(silent) {
        st.hidden = true;
        apply();
        if (opts.onClose) opts.onClose();
        if (!silent) persist();
      }
      function open() {
        st.hidden = false;
        st.collapsed = false;
        apply();
        clampToStage();
        focus();
        persist();
        if (opts.onResize) opts.onResize();
        if (opts.onOpen) opts.onOpen();
      }
      el.querySelector(".tl-close").addEventListener("click", () => close(false));
      el.querySelector(".tl-min").addEventListener("click", () => setCollapsed(!st.collapsed));
      el.querySelector(".tl-zoom").addEventListener("click", () => {
        if (st.zoomRestore) {
          st.rect = st.zoomRestore;
          st.zoomRestore = null;
        } else {
          st.zoomRestore = { ...st.rect };
          st.rect = { ...st.rect, w: opts.zoomW, h: null };
        }
        st.collapsed = false;
        clampToStage();
        persist();
        if (opts.onResize) opts.onResize();
      });

      if (opts.toolbarBtn) {
        opts.toolbarBtn.addEventListener("click", () => (st.hidden ? open() : close(false)));
      }

      const api = {
        el,
        open,
        close,
        clampToStage,
        fitContent() {
          st.rect.h = null;
          st.collapsed = false;
          apply();
          persist();
        },
        isHidden: () => st.hidden,
        bodyWidth: () => Math.max(180, (body.clientWidth || st.rect.w - 2) - 24),
      };
      apply();
      clampToStage();
      if (opts.key) {
        windows.set(opts.key, {
          isDirty: () => userEdited,
          applyRemote(saved) {
            if (userEdited || !saved || !saved.rect || typeof saved.rect !== "object") return;
            const r = saved.rect;
            if (!Number.isFinite(Number(r.x)) || !Number.isFinite(Number(r.y)) ||
                !Number.isFinite(Number(r.w))) return;
            st.rect = {
              x: Number(r.x),
              y: Number(r.y),
              w: Number(r.w),
              h: r.h == null ? null : (Number.isFinite(Number(r.h)) ? Number(r.h) : null),
            };
            st.collapsed = !!saved.collapsed;
            try {
              storage.setItem(store, JSON.stringify({ rect: st.rect, collapsed: st.collapsed }));
            } catch (_) { /* ignore */ }
            clampToStage();
            if (!st.hidden && opts.onResize) opts.onResize();
          },
        });
      }
      return api;
    }

    function hydrate() {
      if (typeof fetchFn !== "function") return Promise.resolve(null);
      return Promise.resolve()
        .then(() => fetchFn(uiSettingsUrl, { cache: "no-store" }))
        .then((res) => (res && res.ok && typeof res.json === "function" ? res.json() : null))
        .then((data) => {
          const root = data && data.settings && typeof data.settings === "object" ? data.settings : data;
          const panels = root && root.panels && typeof root.panels === "object" ? root.panels : null;
          if (!panels) return null;
          for (const [key, saved] of Object.entries(panels)) {
            const win = windows.get(key);
            if (win) win.applyRemote(saved);
            else if (saved && saved.rect) {
              try {
                storage.setItem("nd2wsi.win." + key,
                  JSON.stringify({ rect: saved.rect, collapsed: !!saved.collapsed }));
              } catch (_) { /* ignore */ }
            }
          }
          return panels;
        })
        .catch(() => null);
    }

    return { createWindow, hydrate };
  }

  return { createManager };
});
