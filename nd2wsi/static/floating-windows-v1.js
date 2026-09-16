/* Floating panel geometry, persistence and pointer interactions. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Nd2FloatingWindows = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const clamp = (value, lo, hi) => Math.max(lo, Math.min(hi, value));

  function createManager({ stage, document, storage }) {
    let winZ = 30;

    function createWindow(el, opts) {
      const body = el.querySelector(".win-body");
      const titlebar = el.querySelector(".win-titlebar");
      const store = "nd2wsi.win." + opts.key;

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
        if (saved && saved.rect) {
          st.rect = saved.rect;
          st.collapsed = !!saved.collapsed;
        }
      } catch (_) { /* private mode etc. */ }

      function persist() {
        try {
          storage.setItem(
            store,
            JSON.stringify({ rect: st.rect, collapsed: st.collapsed })
          );
        } catch (_) { /* ignore */ }
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
        st.rect.w = Math.min(Math.max(st.rect.w, opts.minW), opts.maxW, sw - 20);
        st.rect.x = clamp(st.rect.x, 6 - st.rect.w + 60, sw - 60);
        st.rect.y = clamp(st.rect.y, 6, Math.max(6, sh - 34));
        apply();
      }

      function focus() {
        document.querySelectorAll(".mac-window").forEach((w) => w.classList.remove("focused"));
        el.classList.add("focused");
        el.style.zIndex = ++winZ;
      }

      el.addEventListener("pointerdown", focus);

      // -- dragging by the title bar
      let dragFrom = null;
      titlebar.addEventListener("pointerdown", (ev) => {
        if (ev.target.closest(".tl")) return;
        dragFrom = { px: ev.clientX, py: ev.clientY, x: st.rect.x, y: st.rect.y };
        el.classList.add("dragging");
        titlebar.setPointerCapture(ev.pointerId);
        ev.preventDefault();
      });
      titlebar.addEventListener("pointermove", (ev) => {
        if (!dragFrom) return;
        st.rect.x = dragFrom.x + (ev.clientX - dragFrom.px);
        st.rect.y = dragFrom.y + (ev.clientY - dragFrom.py);
        clampToStage();
      });
      titlebar.addEventListener("pointerup", () => {
        dragFrom = null;
        el.classList.remove("dragging");
        persist();
      });
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
          from = {
            px: ev.clientX,
            py: ev.clientY,
            x: st.rect.x,
            y: st.rect.y,
            w: st.rect.w,
            h: st.rect.h != null ? st.rect.h : el.offsetHeight,
          };
          el.classList.add("resizing");
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
          st.rect = r;
          apply();
          if (opts.onResize) opts.onResize();
        });
        h.addEventListener("pointerup", () => {
          from = null;
          el.classList.remove("resizing");
          persist();
        });
      }

      // -- traffic lights
      function setCollapsed(on) {
        st.collapsed = on;
        apply();
        persist();
      }
      function close(silent) {
        st.hidden = true;
        apply();
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
      return api;
    }

    return { createWindow };
  }

  return { createManager };
});
