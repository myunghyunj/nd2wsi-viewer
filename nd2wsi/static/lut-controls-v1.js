/* Channel histogram widgets and live contrast cadence, with explicit viewer callbacks. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Nd2LutControls = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const clamp = (value, lo, hi) => Math.max(lo, Math.min(hi, value));

  function liveUpdate(fn, ms, {
    now = () => performance.now(), setTimer = setTimeout, clearTimer = clearTimeout,
  } = {}) {
    let timer = null;
    let pending = false;
    let last = -Infinity;
    const apply = () => {
      timer = null;
      if (!pending) return;
      pending = false;
      last = now();
      fn();
    };
    const request = () => {
      pending = true;
      if (timer === null)
        timer = setTimer(apply, Math.max(0, ms - (now() - last)));
    };
    request.flush = () => {
      clearTimer(timer);
      apply();
    };
    request.cancel = () => {
      clearTimer(timer);
      timer = null;
      pending = false;
    };
    return request;
  }

  function createWidget({
    channel: ch, dtype, initialLut = null, autoRange: initialAutoRange = false,
    label: winLabel, width, document, window, protocolVersion,
    inkColor, currentTheme, fmtInt, onChange, onFlush, onManualAxis, onShiftChange,
  }) {
    const wrap = document.createElement("div");
    wrap.className = "lut";
    const canvas = document.createElement("canvas");
    canvas.className = "lut-canvas";
    canvas.title = "Scroll to zoom at the pointer; scroll sideways or drag empty space to pan. Double-click for full range. Drag triangles or the round handle to adjust contrast.";
    const ctx = canvas.getContext("2d");
    wrap.append(canvas);

    // size follows the Channels window; relayout() re-derives everything
    let W = 208;
    let H = 84;
    let PLOT = { x0: 4, x1: W - 4, y0: 14, y1: H - 18 };

    function relayout(cssW) {
      W = Math.max(180, Math.round(cssW));
      H = Math.round(Math.max(74, Math.min(150, W * 0.4)));
      PLOT = { x0: 4, x1: W - 4, y0: 14, y1: H - 18 };
      const dpr = window.devicePixelRatio || 1;
      canvas.width = W * dpr;
      canvas.height = H * dpr;
      canvas.style.width = W + "px";
      canvas.style.height = H + "px";
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      draw();
    }

    const def = { lo: ch.window.start, hi: ch.window.end, gamma: 1 };
    const cur = { ...(initialLut || def) };
    // The API supplies source dtype separately from channel windows. Integer
    // looking bounds (notably 0–1) do not identify the pixel representation.
    const discrete = /^(?:u?int\d+|bool|[<>=|]?[iu]\d+)$/.test(String(dtype || ""));
    const spacing = (...values) => Math.max(Number.MIN_VALUE,
      Math.max(...values.map(Math.abs)) * Number.EPSILON);
    const minWindow = (lo, hi) => discrete ? 1 : spacing(lo, hi);
    let rangeMin = Number.isFinite(ch.window.min) ? ch.window.min : def.lo;
    let rangeMax = Number.isFinite(ch.window.max) ? ch.window.max : def.hi;
    if (!(rangeMax > rangeMin)) rangeMax = Math.max(def.hi, rangeMin + minWindow(rangeMin, def.hi));
    const minAxisSpan = () => Math.min(rangeMax - rangeMin,
      Math.max(discrete ? 1 : spacing(rangeMin, rangeMax), (rangeMax - rangeMin) / 65536));
    let vmin = rangeMin;
    let vmax = rangeMax;
    let bins = null;
    let autoHistogram = null;
    let fullHistogram = null;
    let autoRange = !!initialAutoRange;
    let manualAxis = false;

    function projectBins() {
      const histogram = autoRange && autoHistogram ? autoHistogram : fullHistogram;
      if (!histogram) { bins = null; return; }
      if (!manualAxis) { bins = histogram.bins; return; }
      const n = Math.max(64, Math.min(512, Math.round(W - 8)));
      bins = Array(n).fill(0);
      const detail = fullHistogram?.detail;
      const add = (value, count) => {
        if (value < vmin || value > vmax) return;
        const bin = Math.min(n - 1, Math.floor((value - vmin) / (vmax - vmin) * n));
        bins[bin] += count;
      };
      if (detail) {
        detail.values.forEach((value, index) => add(value, detail.counts[index]));
      } else {
        const step = (histogram.vmax - histogram.vmin) / histogram.bins.length;
        histogram.bins.forEach((count, index) => add(histogram.vmin + (index + 0.5) * step, count));
      }
    }

    function updateAxis() {
      const histogram = autoRange && autoHistogram ? autoHistogram : fullHistogram;
      if (!manualAxis) {
        vmin = histogram ? histogram.vmin : rangeMin;
        vmax = histogram ? histogram.vmax : rangeMax;
      }
      projectBins();
      draw();
    }

    function manualView(lo, hi) {
      // Freeze every channel's current axis when leaving automatic fitting.
      // Merely navigating a graph must never change image contrast.
      onManualAxis();
      autoRange = false;
      manualAxis = true;
      const span = clamp(hi - lo, minAxisSpan(), rangeMax - rangeMin);
      vmin = clamp(lo, rangeMin, rangeMax - span);
      vmax = vmin + span;
      projectBins();
      draw();
    }

    const vx = (v) => PLOT.x0 +
      clamp((v - vmin) / (vmax - vmin), 0, 1) * (PLOT.x1 - PLOT.x0);
    const xv = (x) =>
      vmin + clamp((x - PLOT.x0) / (PLOT.x1 - PLOT.x0), 0, 1) * (vmax - vmin);
    const curveY = (t) =>
      PLOT.y1 - Math.pow(Math.max(0, Math.min(1, t)), 1 / cur.gamma) * (PLOT.y1 - PLOT.y0);
    const knobPos = () => ({
      x: vx(cur.lo + (cur.hi - cur.lo) * 0.5),
      y: curveY(0.5),
    });

    function formatValue(value) {
      if (discrete) return fmtInt(value);
      // Keep both tiny signals and small changes at a large offset readable.
      const span = Math.min(vmax - vmin, cur.hi - cur.lo);
      const magnitude = Math.max(Math.abs(vmin), Math.abs(vmax), Math.abs(cur.lo), Math.abs(cur.hi));
      const digits = span > 0 && magnitude > 0
        ? clamp(Math.ceil(Math.log10(magnitude)) - Math.floor(Math.log10(span)) + 3, 6, 17)
        : 6;
      return Number(value.toPrecision(digits)).toString();
    }

    function draw() {
      const w = PLOT.x1 - PLOT.x0;
      const h = PLOT.y1 - PLOT.y0;
      ctx.clearRect(0, 0, W, H);
      // frame
      ctx.strokeStyle = inkColor(0.12);
      ctx.lineWidth = 1;
      ctx.strokeRect(PLOT.x0 - 0.5, PLOT.y0 - 0.5, w + 1, h + 1);
      // histogram (sqrt-scaled so tissue signal shows over background counts)
      if (bins) {
        const peak = Math.sqrt(Math.max(...bins, 1));
        ctx.beginPath();
        ctx.moveTo(PLOT.x0, PLOT.y1);
        for (let b = 0; b < bins.length; b++) {
          const x = PLOT.x0 + (w * (b + 0.5)) / bins.length;
          ctx.lineTo(x, PLOT.y1 - (Math.sqrt(bins[b]) / peak) * (h - 2));
        }
        ctx.lineTo(PLOT.x1, PLOT.y1);
        ctx.closePath();
        ctx.fillStyle = "#" + ch.color + "55";
        ctx.fill();
        ctx.strokeStyle = "#" + ch.color + "cc";
        ctx.stroke();
      }
      // window guides
      ctx.strokeStyle = inkColor(0.30);
      ctx.setLineDash([2, 3]);
      for (const v of [cur.lo, cur.hi].filter((value) => value >= vmin && value <= vmax)) {
        ctx.beginPath();
        ctx.moveTo(vx(v) + 0.5, PLOT.y0);
        ctx.lineTo(vx(v) + 0.5, PLOT.y1);
        ctx.stroke();
      }
      ctx.setLineDash([]);
      // mapping curve: flat-left, gamma ramp, flat-right
      ctx.strokeStyle = inkColor(0.85);
      ctx.beginPath();
      const steps = 40;
      for (let s = 0; s <= steps; s++) {
        const value = vmin + (vmax - vmin) * s / steps;
        const t = (value - cur.lo) / (cur.hi - cur.lo);
        const x = PLOT.x0 + w * s / steps;
        if (s === 0) ctx.moveTo(x, curveY(t));
        else ctx.lineTo(x, curveY(t));
      }
      ctx.stroke();
      // gamma knob
      const k = knobPos();
      if ((cur.lo + cur.hi) / 2 >= vmin && (cur.lo + cur.hi) / 2 <= vmax) {
        ctx.beginPath();
        ctx.arc(k.x, k.y, 4.5, 0, Math.PI * 2);
        ctx.fillStyle = currentTheme() === "light" ? "#f6f6f8" : "#1e1e20";
        ctx.fill();
        ctx.strokeStyle = inkColor(0.85);
        ctx.stroke();
      }
      // lo/hi triangles along the top edge
      if (cur.lo >= vmin && cur.lo <= vmax)
        triangle(vx(cur.lo), currentTheme() === "light" ? "#3a3a3c" : "#111214", inkColor(0.55));
      if (cur.hi >= vmin && cur.hi <= vmax)
        triangle(vx(cur.hi), currentTheme() === "light" ? "#ffffff" : "rgba(255,255,255,0.92)", "rgba(0,0,0,0.6)");
      // labels — SF Mono ramp
      ctx.font = "9px ui-monospace, 'SF Mono', Menlo, monospace";
      ctx.fillStyle = inkColor(0.55);
      ctx.textAlign = "left";
      ctx.fillText(formatValue(cur.lo), PLOT.x0, 9);
      ctx.textAlign = "right";
      ctx.fillText(formatValue(cur.hi), PLOT.x1, 9);
      ctx.textAlign = "center";
      ctx.fillText("G: " + cur.gamma.toFixed(2), (PLOT.x0 + PLOT.x1) / 2, 9);
      ctx.fillStyle = inkColor(0.28);
      ctx.textAlign = "left";
      ctx.fillText(formatValue(vmin), PLOT.x0, H - 3);
      ctx.textAlign = "right";
      ctx.fillText(formatValue(vmax), PLOT.x1, H - 3);
      winLabel.textContent = formatValue(cur.lo) + "–" + formatValue(cur.hi);
      canvas.setAttribute("aria-label", ch.label + " histogram range " + formatValue(vmin) + " to " + formatValue(vmax));
      canvas.setAttribute("data-axis-min", String(vmin));
      canvas.setAttribute("data-axis-max", String(vmax));
    }

    function triangle(x, fill, stroke) {
      ctx.beginPath();
      ctx.moveTo(x - 5, PLOT.y0 - 10);
      ctx.lineTo(x + 5, PLOT.y0 - 10);
      ctx.lineTo(x, PLOT.y0 - 1);
      ctx.closePath();
      ctx.fillStyle = fill;
      ctx.fill();
      ctx.strokeStyle = stroke;
      ctx.stroke();
    }

    function isDefault(l) {
      return (
        l.lo === def.lo && l.hi === def.hi && l.gamma === 1
      );
    }
    function setLut(l) {
      if (!l || ![l.lo, l.hi, l.gamma].every(Number.isFinite)) return false;
      const hi = l.hi > l.lo && (!discrete || isDefault(l)) ? l.hi
        : Math.max(l.hi, l.lo + minWindow(l.lo, l.hi));
      if (!Number.isFinite(hi) || !(hi > l.lo)) return false;
      cur.lo = l.lo;
      cur.hi = hi;
      cur.gamma = Math.max(0.25, Math.min(4, l.gamma));
      draw();
      onChange(isDefault(cur) ? null : { ...cur });
      return true;
    }

    // dragging: lo/hi triangles (horizontal), gamma knob (vertical)
    let mode = null;
    let panFrom = null;
    const pt = (ev) => {
      const r = canvas.getBoundingClientRect();
      return { x: ev.clientX - r.left, y: ev.clientY - r.top };
    };
    canvas.addEventListener("pointerdown", (ev) => {
      if (ev.button !== undefined && ev.button !== 0 && ev.button !== 1) return;
      const p = pt(ev);
      const k = knobPos();
      const loDistance = cur.lo >= vmin && cur.lo <= vmax ? Math.abs(p.x - vx(cur.lo)) : Infinity;
      const hiDistance = cur.hi >= vmin && cur.hi <= vmax ? Math.abs(p.x - vx(cur.hi)) : Infinity;
      const mid = (cur.lo + cur.hi) / 2;
      if (ev.button !== 1 && mid >= vmin && mid <= vmax && Math.hypot(p.x - k.x, p.y - k.y) < 9) mode = "gamma";
      else if (ev.button !== 1 && p.y <= PLOT.y0 + 7 && Math.min(loDistance, hiDistance) < 10)
        mode = hiDistance < loDistance ? "hi" : "lo";
      else mode = "pan";
      panFrom = { x: p.x, lo: vmin, hi: vmax };
      canvas.setPointerCapture(ev.pointerId);
      if (mode !== "pan") drag(ev);
      ev.preventDefault();
    });
    canvas.addEventListener("pointermove", (ev) => {
      if (mode) drag(ev);
    });
    const endDrag = () => {
      if (mode && mode !== "pan") onFlush();
      mode = null;
      panFrom = null;
      canvas.style.cursor = "grab";
    };
    canvas.addEventListener("pointerup", endDrag);
    canvas.addEventListener("pointercancel", endDrag);
    canvas.addEventListener("lostpointercapture", endDrag);

    const navigateWheel = (dx, dy, x) => {
      if (Math.abs(dx) > Math.abs(dy)) {
        const offset = dx / (PLOT.x1 - PLOT.x0) * (vmax - vmin);
        manualView(vmin + offset, vmax + offset);
      } else if (dy) {
        const fraction = clamp((x - PLOT.x0) / (PLOT.x1 - PLOT.x0), 0, 1);
        const anchor = vmin + fraction * (vmax - vmin);
        const span = clamp((vmax - vmin) * Math.exp(clamp(dy * 0.008, -1, 1)),
          minAxisSpan(), rangeMax - rangeMin);
        manualView(anchor - fraction * span, anchor + (1 - fraction) * span);
      }
    };
    canvas.addEventListener("wheel", (ev) => {
      const factor = ev.deltaMode === 1 ? 16 : ev.deltaMode === 2 ? W : 1;
      navigateWheel(ev.deltaX * factor, ev.deltaY * factor, pt(ev).x);
      ev.preventDefault();
      ev.stopPropagation();
    }, { passive: false });
    canvas.addEventListener("dblclick", (ev) => {
      manualView(rangeMin, rangeMax);
      ev.preventDefault();
      ev.stopPropagation();
    });
    const onNativeLutScroll = (ev) => {
      if (ev.origin !== window.location.origin || ev.source !== window.parent) return;
      const data = ev.data;
      if (data?.nd2wsi !== "native-trackpad" || data.version !== protocolVersion) return;
      if (document.elementFromPoint(data.clientX, data.clientY) !== canvas) return;
      // AppKit scrollingDeltaX has the opposite sign from DOM WheelEvent.
      navigateWheel(-Number(data.deltaX || 0), 0, data.clientX - canvas.getBoundingClientRect().left);
    };
    window.addEventListener("message", onNativeLutScroll);
    window.addEventListener("pagehide", () => window.removeEventListener("message", onNativeLutScroll), { once: true });

    function drag(ev) {
      const p = pt(ev);
      if (mode === "pan") {
        const offset = (panFrom.x - p.x) / (PLOT.x1 - PLOT.x0) * (panFrom.hi - panFrom.lo);
        canvas.style.cursor = "grabbing";
        manualView(panFrom.lo + offset, panFrom.hi + offset);
        return;
      }
      const next = { ...cur };
      if (mode === "gamma") {
        const frac = (PLOT.y1 - Math.max(PLOT.y0, Math.min(PLOT.y1, p.y))) /
          (PLOT.y1 - PLOT.y0);
        // knob height = 0.5^(1/gamma)  =>  gamma = ln(0.5)/ln(frac)
        const f = Math.max(0.02, Math.min(0.98, frac));
        next.gamma = Math.max(0.25, Math.min(4, Math.log(0.5) / Math.log(f)));
      } else {
        const v = xv(p.x);
        if (mode === "lo") next.lo = Math.min(v, cur.hi - minWindow(v, cur.hi));
        else next.hi = Math.max(v, cur.lo + minWindow(v, cur.lo));
      }
      if (ev.shiftKey) {
        onShiftChange(next);
      } else {
        setLut(next);
      }
    }

    const widget = {
      element: wrap,
      setLut,
      relayout,
      freezeAxis() { autoRange = false; manualAxis = true; },
      setAutoRange(enabled) {
        autoRange = !!enabled;
        manualAxis = false;
        updateAxis();
      },
      setHistogram(hg) {
        const valid = (value) => value && Array.isArray(value.bins) && value.bins.length &&
          Number.isFinite(value.vmin) && Number.isFinite(value.vmax) && value.vmax > value.vmin;
        if (!valid(hg)) return false;
        fullHistogram = hg;
        autoHistogram = valid(hg.autoHistogram) ? hg.autoHistogram : hg;
        rangeMin = hg.vmin;
        rangeMax = hg.vmax;
        updateAxis();
      },
      clearHistogram() {
        fullHistogram = null;
        autoHistogram = null;
        updateAxis();
      },
      reset() {
        setLut({ ...def });
      },
      auto() {
        const histogram = autoHistogram;
        const window = histogram && autoWindowFromHistogram(histogram.bins, histogram.vmin, histogram.vmax, 0.70, discrete ? 1 : 0);
        if (window) setLut({ ...window, gamma: cur.gamma });
      },
    };
    relayout(width);
    return widget;
  }

  function autoWindowFromHistogram(bins, vmin, vmax, rightPeakFraction = 0.70, minimumWidth = 0) {
    if (!Array.isArray(bins) || !bins.length || !Number.isFinite(vmin) ||
        !Number.isFinite(vmax) || !(vmax > vmin)) return null;
    const total = bins.reduce((sum, count) => sum + Number(count || 0), 0);
    if (!(total > 0)) return null;
    const bw = (vmax - vmin) / bins.length;
    let acc = 0;
    let fallbackBin = -1;
    let highBin = bins.length - 1;
    let modeBin = 0;
    for (let b = 0; b < bins.length; b++) {
      if (Number(bins[b] || 0) > Number(bins[modeBin] || 0)) modeBin = b;
      acc += Number(bins[b] || 0);
      if (acc >= total * 0.001 && fallbackBin < 0) fallbackBin = b;
    }
    acc = 0;
    for (let b = 0; b < bins.length; b++) {
      acc += Number(bins[b] || 0);
      if (acc >= total * 0.999) { highBin = b; break; }
    }
    const fallback = vmin + Math.max(0, fallbackBin) * bw;
    const high = Math.max(vmin + (highBin + 1) * bw, fallback + minimumWidth);
    const mode = vmin + modeBin * bw;
    const low = mode >= fallback + rightPeakFraction * (high - fallback)
      ? fallback
      : mode;
    return { lo: low, hi: Math.max(high, low + minimumWidth) };
  }

  return { createWidget, autoWindowFromHistogram, liveUpdate };
});
