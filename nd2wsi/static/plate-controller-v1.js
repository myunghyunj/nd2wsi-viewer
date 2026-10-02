/* Plate frame controls and their asynchronous request lifetimes.
 * The app owns slide/annotation transitions; this controller owns T/Z,
 * playback, autofocus and cache progress for the current plate.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.Nd2PlateController = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function createController(dependencies, host = globalThis) {
    const {
      state, $, clamp, plateZFor,
      plateZText, platePlaneNote, currentFocusSummary, plateGroupKey,
      platePeriodMs, rawValueLabel, fmtTickLabel, fmtHm,
      fmtPeriodWords, fmtInt, paintPlate, plateFrameChanged,
    } = dependencies;
    const { document, AbortController, Nd2PlateUI: ui } = host;
    // Storage access itself may throw in restricted browser contexts. Keep it
    // inside the existing guarded writes instead of reading it during startup.
    const localStorage = { setItem: (...args) => host.localStorage.setItem(...args) };
    const fetch = (...args) => host.fetch(...args);
    const setInterval = (...args) => host.setInterval(...args);
    const clearInterval = (...args) => host.clearInterval(...args);

    function setPlateZ(z) {
      // reaching for the plane is how the user takes the wheel back from
      // autofocus, so this turns it off
      const pl = state.plate;
      const next = clamp(Math.round(z), 0, state.info.plate.Z - 1);
      const wasAuto = pl.auto;
      if (next === pl.z && !wasAuto) return;
      pl.auto = false;
      if (wasAuto) {
        try { localStorage.setItem("nd2wsi.plate.autofocus", "0"); } catch (_) { /* private mode */ }
      }
      pl.z = next;
      if (wasAuto) {
        renderPlateAuto();
        paintPlate();
      }
      plateFrameChanged();
    }

    function stepPlateZ(delta) {
      // with autofocus on, the plane on screen is the site's own, so a step
      // moves from there rather than from the plane the user last set by hand
      const pl = state.plate;
      const from = pl.auto && pl.focus !== null ? plateZFor(pl.focus) : pl.z;
      setPlateZ(from + ui.zIndexStep(state.info.plate, delta));
    }

    function setPlateAuto(on) {
      const pl = state.plate;
      const next = state.info.plate.Z > 1 && !!on;
      if (next === pl.auto) return;
      pl.auto = next;
      try { localStorage.setItem("nd2wsi.plate.autofocus", next ? "1" : "0"); } catch (_) { /* private mode */ }
      renderPlateAuto();
      paintPlate();
      plateFrameChanged();
    }

    function setPlateT(t) {
      const pl = state.plate;
      const next = clamp(Math.round(t), 0, state.info.plate.T - 1);
      if (next === pl.t) return;
      pl.t = next;
      plateFrameChanged();
    }

    function setPlatePlaying(on) {
      const pl = state.plate;
      pl.playing = state.info.plate.T > 1 && !!on;
      const btn = $("t-play");
      btn.classList.toggle("on", pl.playing);
      btn.title = pl.playing ? "Pause (space)" : "Play (space)";
      btn.innerHTML = pl.playing
        ? '<svg viewBox="0 0 16 16"><path d="M3.5 2.5h3v11h-3zM9.5 2.5h3v11h-3z"/></svg>'
        : '<svg viewBox="0 0 16 16"><path d="M4 2.2v11.6c0 .6.6.9 1.1.6l8.6-5.8c.5-.3.5-1 0-1.3L5.1 1.6C4.6 1.3 4 1.6 4 2.2z"/></svg>';
      clearInterval(pl.timer);
      pl.timer = null;
      if (pl.playing) {
        pl.timer = setInterval(() => {
          const T = state.info.plate.T;
          setPlateT(pl.t + 1 >= T ? 0 : pl.t + 1);
        }, 1000 / pl.fps);
      }
    }

    /* z slider: a track that fills up to the knob, a tick per plane, the home
       plane marked, a value capsule beside the knob */

    function zSliderPct(z) {
      return ui.zSliderPercent(state.info.plate, z);
    }

    function buildZSlider() {
      const pl = state.plate;
      const info = state.info.plate;
      const ticks = $("z-ticks");
      pl.zTickEls = [];
      for (let i = 0; i < info.Z; i++) {
        const tick = document.createElement("div");
        tick.className = "ztick" + (i === info.zHome ? " home" : "");
        tick.style.top = zSliderPct(i) + "%";
        ticks.append(tick);
        pl.zTickEls.push(tick);
      }
      const slider = $("z-slider");
      const track = $("z-track");
      const knob = $("z-knob");
      const zFromY = (y) => {
        const r = track.getBoundingClientRect();
        if (!r.height) return;
        const f = clamp((y - r.top) / r.height, 0, 1);
        setPlateZ(ui.zIndexAtSlider(info, f));
      };
      let drag = false;
      knob.addEventListener("pointerdown", (ev) => {
        drag = true;
        try { knob.setPointerCapture(ev.pointerId); } catch (_) { /* optional */ }
        ev.preventDefault();
      });
      knob.addEventListener("pointermove", (ev) => { if (drag) zFromY(ev.clientY); });
      knob.addEventListener("pointerup", () => { drag = false; });
      knob.addEventListener("pointercancel", () => { drag = false; });
      slider.addEventListener("pointerdown", (ev) => {
        if (ev.target !== knob && ev.target.id !== "z-label") zFromY(ev.clientY);
      });
      knob.addEventListener("keydown", (ev) => {
        if (ev.key === "ArrowUp") { stepPlateZ(1); ev.preventDefault(); ev.stopPropagation(); }
        else if (ev.key === "ArrowDown") { stepPlateZ(-1); ev.preventDefault(); ev.stopPropagation(); }
        else if (ev.key === "Home" || ev.key === "End") {
          setPlateZ(ui.zIndexAtSlider(info, ev.key === "Home" ? 1 : 0));
          ev.preventDefault(); ev.stopPropagation();
        }
      });
      knob.setAttribute("aria-valuemin", "1");
      knob.setAttribute("aria-valuemax", String(info.Z));
    }

    function renderZSlider() {
      const pl = state.plate;
      const info = state.info.plate;
      if (!pl.zTickEls) return;
      // with autofocus on, the slider reports the plane of the site in view
      const shownZ = pl.auto && pl.focus !== null ? plateZFor(pl.focus) : pl.z;
      const pct = zSliderPct(shownZ);
      const offset = pl.auto && pl.focus === null ? null : ui.zOffsetUm(info, shownZ);
      const offsetText = offset === null ? "" : " · " + (offset < 0 ? "−" : "+") + rawValueLabel(Math.abs(offset)) + " µm";
      // the track is inset 10 px inside the slider, so the knob and the label
      // follow the track's own extent
      const slider = $("z-slider");
      const track = $("z-track");
      const sr = slider.getBoundingClientRect();
      const tr = track.getBoundingClientRect();
      const top = sr.height ? (tr.top - sr.top) + (pct / 100) * tr.height : 0;
      pl.zTickEls.forEach((tick, i) => {
        tick.classList.toggle("on", i === shownZ);
        tick.style.top = (sr.height ? (tr.top - sr.top) + (zSliderPct(i) / 100) * tr.height - 10 : 0) + "px";
      });
      const knob = $("z-knob");
      knob.style.top = top + "px";
      knob.setAttribute("aria-valuenow", String(shownZ + 1));
      knob.setAttribute("aria-valuetext", plateZText() + offsetText + (pl.auto && pl.focus === null ? ". Adjust to set a shared manual plane." : ""));
      $("z-slider").classList.toggle("auto", !!pl.auto);
      $("z-fill").style.height = ((1 - pct / 100) * (tr.height || 0)) + "px";
      const label = $("z-label");
      label.style.top = top + "px";
      label.replaceChildren();
      label.append(pl.auto && pl.focus === null ? plateZText() : (shownZ + 1) + " of " + info.Z);
      const dim = document.createElement("span");
      dim.className = "dim";
      let text = offsetText;
      if (pl.auto && pl.focus !== null) text += ui.focusReady(pl.focusMap, pl.t, pl.focus) ? " · auto" : " · manual, AF pending";
      else if (!pl.auto && shownZ === info.zHome) text += " · home";
      dim.textContent = text;
      label.append(dim);
    }

    /* the transport capsule: buttons, a scrubber with ticks at the real frame
       times and a cached band, the clock, the speed */

    function timeSpanMs() {
      const times = state.info.plate.timesMs || [];
      return times.length ? Math.max(0, times[times.length - 1] - times[0]) : 0;
    }

    function timePct(t) {
      const times = state.info.plate.timesMs || [];
      const span = timeSpanMs();
      if (!span || !times.length) return state.info.plate.T > 1 ? t / (state.info.plate.T - 1) * 100 : 0;
      return ((times[t] - times[0]) / span) * 100;
    }

    function buildTimeLine() {
      const pl = state.plate;
      const info = state.info.plate;
      const ticks = $("t-ticks");
      const cached = $("t-cached");
      pl.tickEls = [];
      pl.cachedEls = [];
      const showTicks = info.T <= 120;
      for (let i = 0; i < info.T; i++) {
        if (showTicks) {
          const tick = document.createElement("div");
          tick.className = "ttick";
          tick.style.left = timePct(i) + "%";
          ticks.append(tick);
          pl.tickEls.push(tick);
        }
        const band = document.createElement("span");
        const from = timePct(i);
        const to = i + 1 < info.T ? timePct(i + 1) : 100;
        band.style.left = from + "%";
        band.style.width = Math.max(0, to - from) + "%";
        band.hidden = true;
        cached.append(band);
        pl.cachedEls.push(band);
      }
      // hour labels every 6 h on a long run, every 30 min on a short one
      const labels = $("t-labels");
      const spanMin = timeSpanMs() / 60000;
      if (spanMin > 0) {
        const stepMin = spanMin > 120 ? 360 : 30;
        for (let m = 0; m <= spanMin + 1e-6; m += stepMin) {
          const lab = document.createElement("div");
          const last = m + stepMin > spanMin;
          lab.className = "tlabel" + (m === 0 ? " first" : last ? " last" : "");
          lab.style.left = (m / spanMin) * 100 + "%";
          lab.textContent = m === 0 ? "0" : fmtTickLabel(m);
          labels.append(lab);
        }
      }
      const track = $("t-track");
      track.setAttribute("aria-valuemin", "1");
      track.setAttribute("aria-valuemax", String(info.T));
      track.addEventListener("keydown", (ev) => {
        if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
        const steps = { ArrowLeft: -1, ArrowRight: 1, ArrowDown: -1, ArrowUp: 1, PageDown: -10, PageUp: 10 };
        if (ev.key === "Home") setPlateT(0);
        else if (ev.key === "End") setPlateT(info.T - 1);
        else if (Object.hasOwn(steps, ev.key)) setPlateT(pl.t + steps[ev.key]);
        else return;
        ev.preventDefault(); ev.stopPropagation();
      });
      const tFromX = (x) => {
        const r = track.getBoundingClientRect();
        if (!r.width) return;
        const times = info.timesMs || [];
        if (!timeSpanMs() || times.length !== info.T) {
          setPlateT(Math.round(clamp((x - r.left) / r.width, 0, 1) * (info.T - 1)));
          return;
        }
        const target = times[0] + clamp((x - r.left) / r.width, 0, 1) * timeSpanMs();
        let best = 0;
        for (let i = 1; i < info.T; i++) {
          if (Math.abs(times[i] - target) < Math.abs(times[best] - target)) best = i;
        }
        setPlateT(best);
      };
      let drag = false;
      track.addEventListener("pointerdown", (ev) => {
        drag = true;
        try { track.setPointerCapture(ev.pointerId); } catch (_) { /* optional */ }
        tFromX(ev.clientX);
        ev.preventDefault();
      });
      track.addEventListener("pointermove", (ev) => { if (drag) tFromX(ev.clientX); });
      track.addEventListener("pointerup", () => { drag = false; });
      track.addEventListener("pointercancel", () => { drag = false; });

      $("t-first").onclick = () => setPlateT(0);
      $("t-last").onclick = () => setPlateT(info.T - 1);
      $("t-prev").onclick = () => setPlateT(pl.t - 1);
      $("t-next").onclick = () => setPlateT(pl.t + 1);
      $("t-play").onclick = () => setPlatePlaying(!pl.playing);
      const speed = $("t-speed");
      speed.querySelectorAll("button").forEach((b) => {
        b.disabled = info.T <= 1;
        b.onclick = () => {
          speed.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
          pl.fps = Number(b.dataset.fps) || 8;
          if (pl.playing) setPlatePlaying(true);
        };
      });
      $("t-play").disabled = info.T <= 1;
    }

    function renderPlateAuto() {
      const pl = state.plate;
      const btn = $("t-auto");
      if (!pl || !btn) return;
      const summary = currentFocusSummary();
      btn.hidden = state.info.plate.Z <= 1;
      btn.classList.toggle("on", !!pl.auto);
      btn.setAttribute("aria-pressed", pl.auto ? "true" : "false");
      btn.disabled = state.info.plate.Z <= 1 || (!pl.auto && summary.ready === 0);
      const progress = summary.ready + "/" + summary.total + " ready";
      btn.querySelector("span").textContent = pl.auto ? "AF · " + progress : "Autofocus";
      btn.title = "Autofocus: " + progress + ". Only complete Z stacks are applied; other sites use the manual plane. "
        + "Scoring: mean image of all raw channels. (F)";
    }

    function loadPlateFocus() {
      // the sharpest plane of every site at every time point, measured from the
      // store's own reductions. An extra, so a failure only leaves it off
      const pl = state.plate;
      if (!pl || pl.focusRequest) return;
      const generation = state.info.generation;
      const controller = new AbortController();
      pl.focusRequest = controller;
      fetch("api/plate/focus", { cache: "no-store", signal: controller.signal })
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.status))))
        .then((map) => {
          if (controller.signal.aborted || state.plate !== pl || state.info.generation !== generation || pl.focusRequest !== controller
              || !map || !Array.isArray(map.best) || !Array.isArray(map.complete)) return;
          // the map keeps growing while the planes are measured, so a repaint
          // only happens when the planes on screen actually move; reloading the
          // deep-zoom stage on every poll would blank it on a two second beat
          const before = state.info.plate.sites.map((site) => plateZFor(site.i)).join(",");
          pl.focusMap = map;
          renderPlateAuto();
          const after = state.info.plate.sites.map((site) => plateZFor(site.i)).join(",");
          if (pl.auto && before !== after) {
            paintPlate();
            plateFrameChanged();
          } else {
            renderZSlider();
            $("plane-note").textContent = platePlaneNote();
          }
        })
        .catch(() => {})
        .finally(() => { if (pl.focusRequest === controller) pl.focusRequest = null; });
    }

    function pollPlateStatus() {
      // the store beside the file fills in the background; a band under the
      // scrubber shows which time points are in, and the status bar counts,
      // so the series can be scrubbed without touching the ND2 once it is done
      const pl = state.plate;
      if (!pl || pl.statusTimer || pl.statusRequest) return;
      const generation = state.info.generation;
      let closed = false;
      const cell = $("plate-cache-cell");
      const stop = () => {
        closed = true;
        clearInterval(pl.statusTimer);
        pl.statusTimer = null;
        pl.statusRequest?.abort();
      };
      const ask = () => {
        if (closed || state.plate !== pl || state.info.generation !== generation) { stop(); return; }
        if (pl.statusRequest) return;
        const controller = new AbortController();
        pl.statusRequest = controller;
        fetch("api/plate/status", { cache: "no-store", signal: controller.signal })
          .then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.status))))
          .then((d) => {
            if (closed || state.plate !== pl || state.info.generation !== generation) { stop(); return; }
            pl.storePerT = Array.isArray(d.perT) ? d.perT : null;
            pl.storeDone = Number(d.done) || 0;
            pl.storeTotal = Number(d.total) || 0;
            renderTimeLine();
            const done = pl.storeTotal && pl.storeDone >= pl.storeTotal;
            // Agent windows may read directly without a store. Nothing is being
            // built in that case; reporting 0% would promise progress that cannot occur.
            const noStore = d.path === null && !d.building && !d.writer;
            if (cell) {
              cell.hidden = !noStore && (done || !pl.storeTotal);
              cell.title = noStore ? "This image is open without a viewing cache." : "";
              $("plate-cache-val").textContent = noStore ? "No viewing cache"
                : pl.storeTotal
                  ? Math.floor((pl.storeDone / pl.storeTotal) * 100) + " % · " + fmtInt(pl.storeDone) + " of " + fmtInt(pl.storeTotal)
                  : "";
            }
            if (noStore) { stop(); return; }
            const map = pl.focusMap;
            const focusNeeded = state.info.plate.Z > 1;
            const measured = map && map.completeCount >= map.total;
            if (focusNeeded && !measured) loadPlateFocus();
            if (done && (!focusNeeded || measured)) {
              stop();
            }
          })
          .catch(() => {})
          .finally(() => { if (pl.statusRequest === controller) pl.statusRequest = null; });
      };
      pl.statusTimer = setInterval(ask, 2000);
      ask();
      host.addEventListener("pagehide", stop, { once: true });
    }

    function renderTimeLine() {
      const pl = state.plate;
      const info = state.info.plate;
      const times = info.timesMs || [];
      const perFrame = (info.P || 1) * (info.Z || 1);
      (pl.tickEls || []).forEach((tick, i) => tick.classList.toggle("on", i === pl.t));
      (pl.cachedEls || []).forEach((band, i) => {
        const seen = pl.loaded.get(i + "/" + plateGroupKey());
        const stored = pl.storePerT && pl.storePerT[i] >= perFrame;
        band.hidden = !(stored || !!(seen && seen.size >= info.P));
      });
      const pct = timePct(pl.t);
      $("t-playhead").style.left = pct + "%";
      $("t-fill").style.width = pct + "%";
      $("t-read-clock").textContent = fmtHm(times.length ? times[pl.t] - times[0] : 0);
      const period = platePeriodMs();
      $("t-read-frame").textContent = "frame " + (pl.t + 1) + " of " + info.T +
        (period ? " · every " + fmtPeriodWords(period) : "");
      const track = $("t-track");
      track.setAttribute("aria-valuenow", String(pl.t + 1));
      track.setAttribute("aria-valuetext", (times.length ? fmtPeriodWords(times[pl.t] - times[0]) + ", " : "")
        + "frame " + (pl.t + 1) + " of " + info.T);
      track.setAttribute("aria-disabled", String(info.T <= 1));
      $("t-prev").disabled = pl.t === 0;
      $("t-first").disabled = pl.t === 0;
      $("t-next").disabled = pl.t >= info.T - 1;
      $("t-last").disabled = pl.t >= info.T - 1;
      $("t-play").disabled = info.T <= 1;
      renderPlateAuto();
    }

    return {
      setPlateZ, stepPlateZ, setPlateAuto, setPlateT,
      setPlatePlaying, buildZSlider, renderZSlider, buildTimeLine,
      renderTimeLine, renderPlateAuto, loadPlateFocus, pollPlateStatus,
    };
  }

  return { createController };
});
