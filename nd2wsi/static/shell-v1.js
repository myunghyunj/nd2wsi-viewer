"use strict";

const $ = (id) => document.getElementById(id);
const frames = new Map();
let slides = [];
let active = null;
let busyTab = null;
let busyTimer = null;
let toastTimer = null;

function showError(message) {
  const toast = $("shell-toast");
  toast.textContent = String(message || "Unknown error");
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 4200);
}

function applyShellTheme(theme, mode) {
  document.documentElement.classList.toggle("light", theme === "light");
  try {
    localStorage.setItem("nd2wsi.theme", theme);
    if (mode) localStorage.setItem("nd2wsi.theme.mode", mode);
  } catch (_) { /* private mode */ }
}

try {
  const mode = localStorage.getItem("nd2wsi.theme.mode");
  const theme = mode === "light" || mode === "dark"
    ? mode
    : matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  // apply without writing: recording the system preference here would
  // later read back as if the user had chosen it explicitly
  document.documentElement.classList.toggle("light", theme === "light");
} catch (_) { /* private mode */ }

function render() {
  const bar = $("tabbar");
  bar.querySelectorAll(".tab").forEach((tab) => tab.remove());
  const plus = $("newtab");

  function makeTab(slide, busy) {
    const tab = document.createElement("div");
    tab.className = "tab" + (busy ? " busy" : slide.sid === active ? " active" : "");
    tab.title = slide.name;

    const dot = document.createElement("span");
    dot.className = "dot";
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = slide.name;
    tab.append(dot, name);

    if (busy) {
      const progress = document.createElement("div");
      progress.className = "tprog";
      const fill = document.createElement("span");
      if (slide.pct > 0) fill.style.width = `${slide.pct}%`;
      else fill.className = "indet";
      progress.append(fill);
      tab.append(progress);
    } else {
      const close = document.createElement("button");
      close.className = "x";
      close.type = "button";
      close.textContent = "×";
      close.title = "Close tab";
      close.onclick = (event) => {
        event.stopPropagation();
        closeTab(slide.sid);
      };
      tab.append(close);
      tab.onclick = () => activate(slide.sid);
    }
    bar.insertBefore(tab, plus);
  }

  slides.forEach((slide) => makeTab(slide, false));
  if (busyTab) makeTab(busyTab, true);
  $("empty").hidden = slides.length > 0 || Boolean(busyTab);
  document.title = active
    ? `${(slides.find((slide) => slide.sid === active) || {}).name || "Slide"} — nd2wsi-viewer`
    : "nd2wsi-viewer";
}

function activate(sid) {
  active = sid;
  if (sid && !frames.has(sid)) {
    const frame = document.createElement("iframe");
    frame.src = `s/${sid}/`;
    frame.title = slides.find((slide) => slide.sid === sid)?.name || "Slide";
    frames.set(sid, frame);
    $("frames").append(frame);
  }
  for (const [key, frame] of frames) frame.classList.toggle("active", key === active);
  const selected = frames.get(active);
  if (selected?.contentWindow) {
    selected.contentWindow.postMessage({ nd2wsi: "theme-request" }, location.origin);
  }
  render();
}

function refresh(selectSid) {
  return fetch("api/slides", { cache: "no-store" })
    .then((response) => response.json())
    .then((data) => {
      slides = data.slides || [];
      for (const [key, frame] of [...frames]) {
        if (!slides.some((slide) => slide.sid === key)) {
          frame.remove();
          frames.delete(key);
        }
      }
      if (selectSid) activate(selectSid);
      else if (!slides.some((slide) => slide.sid === active)) {
        activate(slides.length ? slides[slides.length - 1].sid : null);
      } else render();
    })
    .catch((error) => showError(`Could not refresh slides: ${error}`));
}

function openMany(paths) {
  return paths.reduce((chain, path) => chain.then(() => openPath(path)), Promise.resolve());
}

function openPath(path) {
  const job = Math.random().toString(36).slice(2, 10);
  const name = path.split("/").pop();
  busyTab = { name: `Opening ${name}…`, job, pct: null };
  render();

  clearInterval(busyTimer);
  busyTimer = setInterval(() => {
    fetch(`api/roi/progress?job=${job}`, { cache: "no-store" })
      .then((response) => response.json())
      .then((data) => {
        if (!busyTab || data.state !== "converting") return;
        busyTab.pct = data.pct || 0;
        busyTab.name = busyTab.pct > 0
          ? `Converting ${name} ${busyTab.pct} %`
          : `Preparing ${name}…`;
        render();
      })
      .catch(() => {});
  }, 400);

  return fetch("api/open", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, job }),
  })
    .then((response) => response.json())
    .then((data) => {
      clearInterval(busyTimer);
      busyTab = null;
      if (data.error) {
        render();
        showError(data.error);
        return;
      }
      return refresh(data.sid);
    })
    .catch((error) => {
      clearInterval(busyTimer);
      busyTab = null;
      render();
      showError(`Open failed: ${error}`);
    });
}

function closeTab(sid) {
  fetch("api/close", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sid }),
  })
    .then((response) => response.json())
    .then((data) => data.error ? showError(data.error) : refresh())
    .catch((error) => showError(`Close failed: ${error}`));
}

$("newtab").onclick = () => {
  if (window.pywebview?.api?.pick_paths) {
    window.pywebview.api.pick_paths().then((paths) => {
      if (paths?.length) openMany(paths);
    });
    return;
  }
  const path = prompt("Path to an ND2, SVS, or OME-Zarr store:");
  if (path?.trim()) openPath(path.trim());
};

const zone = $("dropzone");
window.__pydrop_multi = (paths) => {
  zone.hidden = true;
  openMany(paths);
};
window.__pydrop = (path) => window.__pydrop_multi([path]);
window.addEventListener("dragenter", (event) => {
  const types = event.dataTransfer ? Array.from(event.dataTransfer.types || []) : [];
  if (types.includes("Files")) zone.hidden = false;
});
window.addEventListener("dragover", (event) => event.preventDefault());
zone.addEventListener("dragleave", (event) => {
  if (event.target === zone) zone.hidden = true;
});
zone.addEventListener("drop", (event) => {
  event.preventDefault();
  zone.hidden = true;
  if (window.pywebview) return;
  const paths = Array.from(event.dataTransfer.files)
    .map((file) => file.pywebviewFullPath || file.path)
    .filter(Boolean);
  if (paths.length) openMany(paths);
  else showError("The browser hid the dropped file paths. Use + or the macOS app.");
});
window.addEventListener("drop", (event) => event.preventDefault());
document.addEventListener("dragover", (event) => event.preventDefault(), true);
document.addEventListener("drop", (event) => event.preventDefault(), true);

window.addEventListener("message", (event) => {
  if (event.origin !== location.origin || !event.data) return;
  if (event.data.nd2wsi === "slide-trashed") refresh();
  if (event.data.nd2wsi === "file-drag") zone.hidden = false;
  if (event.data.nd2wsi === "theme") {
    applyShellTheme(event.data.theme, event.data.mode);
    for (const [, frame] of frames) {
      if (frame.contentWindow && frame.contentWindow !== event.source) {
        frame.contentWindow.postMessage(
          { nd2wsi: "theme", mode: event.data.mode },
          location.origin,
        );
      }
    }
  }
});

refresh();
