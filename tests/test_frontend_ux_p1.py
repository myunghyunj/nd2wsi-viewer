"""Phase 1 frontend UX polish, toolbar discoverability, channel colors, solo, scene picker, undo/redo & shortcuts."""

from __future__ import annotations

import json
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from node_host import NODE, run_node

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "nd2wsi" / "static"

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

MODIFIED_JS_FILES = (
    "shell-v1.js",
    "app.js",
    "floating-windows-v1.js",
    "lut-persistence-v1.js",
    "shortcut-router-v1.js",
)


class HtmlInspector(HTMLParser):
    def __init__(self, html: str):
        super().__init__()
        self.ids: dict[str, dict] = {}
        self.stack: list[tuple[str, str | None]] = []
        self._capture_id: str | None = None
        self._texts: dict[str, list[str]] = {}
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        attr_map = dict(attrs)
        el_id = attr_map.get("id")
        if el_id:
            self.ids[el_id] = {
                "tag": tag,
                "attrs": attr_map,
                "classes": set((attr_map.get("class") or "").split()),
                "ancestors": [item[1] for item in self.stack if item[1]],
            }
            self._texts[el_id] = []
        if tag not in {"meta", "link", "input", "br", "img", "hr"}:
            self.stack.append((tag, el_id))

    def handle_data(self, data: str):
        for _, el_id in self.stack:
            if el_id in self._texts:
                self._texts[el_id].append(data)

    def handle_endtag(self, tag: str):
        for idx in range(len(self.stack) - 1, -1, -1):
            if self.stack[idx][0] == tag:
                self.stack = self.stack[:idx]
                break

    def text_of(self, el_id: str) -> str:
        return "".join(self._texts.get(el_id, [])).strip()


@pytest.mark.parametrize("filename", MODIFIED_JS_FILES)
def test_js_syntax_check_passes_for_all_modified_scripts(filename: str):
    path = STATIC / filename
    result = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, f"node --check failed for {filename}: {result.stderr}"


def test_boot_html_inline_script_syntax_and_recent_files_workflow(tmp_path: Path):
    boot_html = (STATIC / "boot.html").read_text(encoding="utf-8")
    match = re.search(r"<script>([\s\S]*?)</script>", boot_html)
    assert match is not None
    script_file = tmp_path / "boot_inline.js"
    script_file.write_text(match.group(1), encoding="utf-8")
    syntax = subprocess.run([NODE, "--check", str(script_file)], capture_output=True, text=True, check=False)
    assert syntax.returncode == 0, syntax.stderr

    # Verify drop prompt format coverage in both boot.html and shell.html
    shell_html = (STATIC / "shell.html").read_text(encoding="utf-8")
    for page_name, html in (("boot.html", boot_html), ("shell.html", shell_html)):
        for ext in (".nd2", ".czi", ".lif", ".tif", ".ome.tif", ".svs", ".sdpc", ".nd2svs", ".jpg", ".png"):
            assert ext in html, f"{ext} missing from {page_name}"

    # Verify Recent Files rendering, unavailable filtering, silent 404 handling, and click-to-open
    harness = r"""
const fs = require('fs'), vm = require('vm');
const html = fs.readFileSync(process.argv[1], 'utf8');
const code = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const mode = process.argv[2];

function makeNode(id = '') {
  return {
    id, hidden: id === 'recent-files', disabled: false, textContent: '', title: '', className: '',
    style: {}, children: [], handlers: {},
    classList: { add() {}, remove() {} },
    append(...xs) { this.children.push(...xs); },
    replaceChildren(...xs) { this.children = [...xs]; },
    addEventListener(k, fn) { this.handlers[k] = fn; },
  };
}
const nodes = new Map(['drop', 'status', 'bar', 'fill', 'pct', 'update', 'recent-files', 'recent-list']
  .map(id => [id, makeNode(id)]));
const opened = [];
let dialogClicks = 0;

const fetchImpl = async (url, opts) => {
  if (mode === '404') return { ok: false, status: 404, json: async () => ({}) };
  if (url.endsWith('/api/recent-files')) {
    return {
      ok: true, status: 200,
      json: async () => ({
        ok: true,
        items: [
          { path: '/data/brain_scan.ome.tif', name: 'brain_scan.ome.tif', available: true },
          { path: '/Volumes/Unmounted/old.nd2', name: 'old.nd2', available: false },
          { path: '/data/organoid.lif', name: 'organoid.lif', available: true },
        ],
      }),
    };
  }
  return { ok: true, json: async () => ({}) };
};

const ctx = vm.createContext({
  document: {
    getElementById: id => nodes.get(id),
    createElement: tag => makeNode(tag),
    addEventListener() {},
  },
  window: {
    addEventListener() {},
    pywebview: {
      api: {
        open_slide: async () => { dialogClicks++; return '/tok/'; },
        open_paths: async (paths) => { opened.push(paths); return null; },
        status: async () => '',
        progress: async () => -1,
      },
    },
  },
  pywebview: null,
  location: { pathname: '/', replace() {} },
  fetch: fetchImpl,
  setInterval: () => 1, clearInterval() {}, setTimeout() {}, Promise,
});
ctx.window.window = ctx.window;
ctx.pywebview = ctx.window.pywebview;
vm.runInContext(code, ctx);
(async () => {
  for (let i = 0; i < 10; i++) await Promise.resolve();
  const list = nodes.get('recent-list');
  if (list.children.length) {
    let propagated = true;
    list.children[0].handlers.click({ stopPropagation() { propagated = false; } });
    for (let i = 0; i < 10; i++) await Promise.resolve();
    process.stdout.write(JSON.stringify({
      hidden: nodes.get('recent-files').hidden,
      names: list.children.map(b => b.children[0].textContent),
      opened, dialogClicks, propagated,
    }));
  } else {
    process.stdout.write(JSON.stringify({
      hidden: nodes.get('recent-files').hidden,
      names: [], opened, dialogClicks,
    }));
  }
})();
"""
    ok_res = json.loads(run_node(harness, STATIC / "boot.html", "ok", check=True).stdout)
    assert ok_res["hidden"] is False
    assert ok_res["names"] == ["brain_scan.ome.tif", "organoid.lif"]
    assert ok_res["opened"] == [["/data/brain_scan.ome.tif"]]
    assert ok_res["dialogClicks"] == 0 and ok_res["propagated"] is False

    err_res = json.loads(run_node(harness, STATIC / "boot.html", "404", check=True).stdout)
    assert err_res["hidden"] is True and err_res["names"] == []


def test_shell_toolbar_default_expansion_compare_svg_and_ui_settings_sync():
    shell_doc = HtmlInspector((STATIC / "shell.html").read_text(encoding="utf-8"))
    shell_raw = (STATIC / "shell.html").read_text(encoding="utf-8")
    compare_markup = shell_raw.split('id="compare-toggle"', 1)[1].split("</button>", 1)[0]
    assert 'class="compare-icon"' in compare_markup
    assert "share-icon" not in compare_markup
    assert "compare-toggle" in shell_doc.ids

    script = r"""
const fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(process.argv[1] + '/shell-v1.js', 'utf8');
const cfg = JSON.parse(process.argv[2]);
const storage = new Map(Object.entries(cfg.storage || {}));
const posts = [];
const els = new Map();
const el = id => {
  if (!els.has(id)) {
    const attrs = {};
    els.set(id, {
      id, hidden: id === 'shell-tools', disabled: false, title: '', textContent: '',
      dataset: {}, style: {},
      classList: { toggle() {}, add() {}, remove() {}, contains: () => false },
      setAttribute(k, v) { attrs[k] = v; },
      getAttribute(k) { return attrs[k]; },
      contains: () => false, focus() {}, addEventListener() {},
    });
  }
  return els.get(id);
};
const ctx = vm.createContext({
  $: el,
  document: { getElementById: el, activeElement: null, addEventListener() {},
    documentElement: { classList: { toggle() {} }, style: { setProperty() {} } } },
  window: { innerWidth: cfg.width, addEventListener() {} },
  localStorage: { getItem: k => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, String(v)) },
  fetch: async (url, opts = {}) => {
    if (opts.method === 'POST') { posts.push(JSON.parse(opts.body)); return { ok: true, json: async () => ({ ok: true }) }; }
    if (cfg.remoteExpanded === undefined) return { ok: false, status: 404 };
    return { ok: true, json: async () => ({ ok: true, shellToolsExpanded: cfg.remoteExpanded }) };
  },
  pairPicker: { open: false }, compare: { states: new Map() }, readyFrames: new Set(),
  active: null, quitPreparation: null,
  closePairPicker() {}, openActiveTranslucent() {}, panelShortcut() {}, refreshShellToolButtons() {},
});
const start = source.indexOf('function setShellToolsExpanded(');
const end = source.indexOf('function refreshAstromapButton(', start);
vm.runInContext(source.slice(start, end), ctx);
(async () => {
  const initialHidden = el('shell-tools').hidden;
  for (let i = 0; i < 8; i++) await Promise.resolve();
  const afterHydrateHidden = el('shell-tools').hidden;
  if (cfg.toggleClick) {
    el('shell-tools-toggle').onclick();
    for (let i = 0; i < 6; i++) await Promise.resolve();
  }
  process.stdout.write(JSON.stringify({
    initialHidden, afterHydrateHidden, finalHidden: el('shell-tools').hidden,
    posts, cached: storage.get('nd2wsi.shellToolsExpanded') ?? null,
  }));
})();
"""
    wide = json.loads(run_node(script, STATIC, json.dumps({"width": 1280}), check=True).stdout)
    assert wide["initialHidden"] is False and wide["afterHydrateHidden"] is False

    narrow = json.loads(run_node(script, STATIC, json.dumps({"width": 820}), check=True).stdout)
    assert narrow["initialHidden"] is True

    persisted_collapsed = json.loads(
        run_node(script, STATIC, json.dumps({"width": 1440, "remoteExpanded": False}), check=True).stdout
    )
    assert persisted_collapsed["initialHidden"] is False
    assert persisted_collapsed["afterHydrateHidden"] is True
    assert persisted_collapsed["cached"] == "false"

    toggled = json.loads(
        run_node(script, STATIC, json.dumps({"width": 1280, "toggleClick": True}), check=True).stdout
    )
    assert toggled["finalHidden"] is True
    assert toggled["posts"] == [{"shellToolsExpanded": False}]


def test_channel_swatch_popover_col_query_lut_color_persistence_and_alt_solo():
    script = r"""
const fs = require('fs'), path = require('path'), vm = require('vm');
const source = fs.readFileSync(path.join(process.argv[1], 'app.js'), 'utf8');
const LutControls = require(path.join(process.argv[1], 'lut-controls-v1.js'));
const LutTransfer = require(path.join(process.argv[1], 'lut-transfer-v1.js'));
const LutPersistence = require(path.join(process.argv[1], 'lut-persistence-v1.js'));
const ShortcutRouter = require(path.join(process.argv[1], 'shortcut-router-v1.js'));

const canvasCtx = new Proxy({}, { get: (o, k) => o[k] || (() => {}) });
const allNodes = [];
function makeNode(tag, id = '') {
  const classes = new Set();
  const attrs = {};
  const handlers = {};
  const node = {
    tag, id, hidden: false, disabled: false, textContent: '', title: '', value: '',
    dataset: {}, style: {}, children: [], parentNode: null, attrs, handlers,
    get className() { return [...classes].join(' '); },
    set className(v) { classes.clear(); String(v || '').split(/\s+/).filter(Boolean).forEach(c => classes.add(c)); },
    classList: {
      add: (...cs) => cs.forEach(c => classes.add(c)),
      remove: (...cs) => cs.forEach(c => classes.delete(c)),
      contains: c => classes.has(c),
      toggle(c, on) { const next = on === undefined ? !classes.has(c) : !!on; next ? classes.add(c) : classes.delete(c); return next; },
    },
    append(...xs) { xs.forEach(x => { if (x && typeof x === 'object') x.parentNode = this; this.children.push(x); }); },
    replaceChildren(...xs) { this.children = []; this.append(...xs); },
    remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(c => c !== this); },
    setAttribute(k, v) { attrs[k] = String(v); },
    getAttribute(k) { return attrs[k] ?? null; },
    addEventListener(k, fn) { handlers[k] = fn; },
    removeEventListener(k, fn) { if (handlers[k] === fn) delete handlers[k]; },
    getContext: () => canvasCtx,
    setPointerCapture() {},
    getBoundingClientRect: () => ({ left: 20, top: 40, bottom: 52, right: 32, width: 12, height: 12 }),
    contains(other) { for (let cur = other; cur; cur = cur.parentNode) if (cur === this) return true; return false; },
    focus() {},
    click() { if (this.onclick) this.onclick({}); else if (handlers.click) handlers.click({}); },
  };
  allNodes.push(node);
  return node;
}

const byId = new Map();
for (const id of ['channel-list', 'lut-auto-range', 'lut-type', 'lut-copy', 'lut-paste',
                  'lut-cancel', 'lut-dialog', 'lut-form', 'lut-fields', 'lut-error',
                  'lut-type-tooltip', 'lut-copy-tooltip', 'lut-paste-tooltip', 'win-channels']) {
  const n = makeNode('div', id);
  if (id.endsWith('-tooltip')) n.hidden = true;
  byId.set(id, n);
}
const body = makeNode('body');
const savedPayloads = [];
let remoteSaved = null;

const state = {
  info: {
    name: 'multich.nd2', rgb: false, dtype: 'uint16', generation: 'g42', maxRenderMpx: 100,
    levels: [{ path: '0', downsample: 1 }],
    channels: [
      { label: 'DAPI', color: '0000FF', window: { min: 0, max: 4095, start: 100, end: 2000 } },
      { label: 'GFP', color: '00FF00', window: { min: 0, max: 4095, start: 50, end: 1800 } },
      { label: 'CY5', color: 'FF0000', window: { min: 0, max: 4095, start: 80, end: 2500 } },
    ],
  },
  channels: [0, 1, 2], soloPrevChannels: null, luts: [null, null, null], lutWidgets: [],
  lutAutoRange: false, histogram: { failed: false },
  roi: { x: 0, y: 0, w: 200, h: 200 },
  windows: { channels: { bodyWidth: () => 240 } },
};

const downloads = [];
const fetchFn = async (url, opts = {}) => {
  if (opts.method === 'POST') {
    remoteSaved = JSON.parse(opts.body);
    savedPayloads.push(remoteSaved);
    return { ok: true, json: async () => ({ ok: true }) };
  }
  return { ok: true, json: async () => ({ ok: true, available: true, settings: remoteSaved }) };
};

const win = {
  Nd2LutControls: LutControls, Nd2LutTransfer: LutTransfer,
  Nd2LutPersistence: LutPersistence, Nd2ShortcutRouter: ShortcutRouter,
  devicePixelRatio: 1, innerWidth: 1280, innerHeight: 800,
  addEventListener() {}, removeEventListener() {},
};
const ctx = vm.createContext({
  state, window: win, URLSearchParams, Math, Number, String, Boolean, Array, Set, Object, JSON, Promise,
  $: id => byId.get(id),
  document: {
    body,
    createElement: tag => {
      const n = makeNode(tag);
      if (tag === 'a') n.click = function () { downloads.push(this.href); };
      return n;
    },
    addEventListener() {}, removeEventListener() {},
  },
  fetch: fetchFn, setTimeout: (fn) => { fn(); return 1; }, clearTimeout() {},
  VIEWPORT_PROTOCOL_VERSION: 2, inkColor: () => '#fff', currentTheme: () => 'dark', fmtInt: String,
  currentSlideSid: () => 'sid1', showToast() {},
  loadHistograms() {}, refreshTiles() {}, scheduleScaleStrip() {}, trackExport() {},
  activeFrameContext: () => ({ frame: null }), frameOwnsRoi: () => true,
  pixelSize: () => [0.5, 0.5], roiScale: () => ({ d: 1, w: 200, h: 200 }), levelPathFor: () => '0',
  appendFrameParams: q => q, scopedButton() {},
  applyLuts: Object.assign(() => {}, { flush() {} }),
});

const slice = (a, b) => source.slice(source.indexOf(a), source.indexOf(b, source.indexOf(a)));
vm.runInContext(
  slice('async function initializeDisplayLuts(', 'function acceptLutHistograms(') +
  slice('function lutParam(', 'function activeFrameParams(') +
  slice('function macSwitch(', '/* ---- per-channel LUT') +
  slice('function buildLutRow(', 'function setHistogramReady(') +
  slice('function scaleBarExportIssue(', '/* Poll the server'),
  ctx
);

(async () => {
  await ctx.initializeDisplayLuts();
  ctx.buildChannelPanel();

  const swatches = allNodes.filter(n => n.tag === 'button' && n.classList.contains('swatch'));
  const defaultQuery = ctx.renderParams(new URLSearchParams()).toString();

  // Open swatch popover on channel 0 and pick Cyan (#00FFFF); channel 2 -> Magenta (#FF00FF)
  swatches[0].handlers.click({ preventDefault() {}, stopPropagation() {} });
  const pop = body.children.find(n => n.classList.contains('swatch-popover'));
  const presets = pop.children[1].children;
  const presetHexes = presets.map(p => p.dataset.hex);
  presets[0].handlers.click(); // Cyan 00FFFF

  swatches[2].handlers.click({ preventDefault() {}, stopPropagation() {} });
  const pop2 = body.children.find(n => n.classList.contains('swatch-popover'));
  pop2.children[1].children[1].handlers.click(); // Magenta FF00FF

  await ctx.state.lutPersistence.flush();
  const customQuery = ctx.renderParams(new URLSearchParams()).toString();

  // Export rendered vs raw ROI URLs
  for (const [fmt, bar] of [['png', false], ['jpg', true], ['svg', true], ['tiff', false], ['nd2', false]]) {
    ctx.downloadRoi(fmt, bar);
  }

  // Simulate fresh reopen restoring persisted channel colors from api/lut-settings
  state.info.channels[0].color = '0000FF';
  state.info.channels[2].color = 'FF0000';
  state.luts = [null, null, null];
  await ctx.initializeDisplayLuts();
  const restoredColors = state.info.channels.map(c => c.color);

  // Reset channel 0 and channel 2 back to metadata via popover Reset button
  swatches[0].handlers.click({ preventDefault() {}, stopPropagation() {} });
  body.children.find(n => n.classList.contains('swatch-popover')).children[3].handlers.click();
  swatches[2].handlers.click({ preventDefault() {}, stopPropagation() {} });
  body.children.find(n => n.classList.contains('swatch-popover')).children[3].handlers.click();
  const afterResetQuery = ctx.renderParams(new URLSearchParams()).toString();

  // Test Option/Alt-click solo channel & restore previous multi-channel state
  const switches = allNodes.filter(n => n.attrs.role === 'switch');
  switches[2].handlers.click({}); // turn off channel 2 -> [0, 1] active
  const beforeSolo = [...state.channels];
  switches[1].handlers.click({ altKey: true }); // Alt-click channel 1 -> solo [1]
  const soloed1 = [...state.channels];
  const ariaDuringSolo = switches.map(s => s.attrs['aria-checked']);
  switches[2].handlers.click({ altKey: true }); // Alt-click channel 2 while soloed -> solo [2], keeping [0, 1] snapshot
  const soloed2 = [...state.channels];
  switches[2].handlers.click({ altKey: true }); // Alt-click already-soloed channel 2 -> restores [0, 1]!
  const restoredFromSolo = [...state.channels];

  process.stdout.write(JSON.stringify({
    swatchCount: swatches.length,
    presetHexes,
    defaultQuery,
    customQuery,
    downloads,
    savedLuts: savedPayloads[0]?.luts,
    restoredColors,
    afterResetQuery,
    beforeSolo, soloed1, ariaDuringSolo, soloed2, restoredFromSolo,
  }));
})();
"""
    out = json.loads(run_node(script, STATIC, check=True).stdout)
    assert out["swatchCount"] == 3
    assert out["presetHexes"] == ["00FFFF", "FF00FF", "FFFF00", "00FF00", "FF3B30", "3B82F6", "FFFFFF"]
    assert "col=" not in out["defaultQuery"]
    assert parse_qs(out["customQuery"])["col"] == ["00FFFF,,FF00FF"]
    assert out["savedLuts"][0]["color"] == "00FFFF"
    assert "color" not in out["savedLuts"][1]
    assert out["savedLuts"][2]["color"] == "FF00FF"
    assert out["restoredColors"] == ["00FFFF", "00FF00", "FF00FF"]
    assert "col=" not in out["afterResetQuery"]

    # Rendered & scalebar exports carry `col`, raw TIFF/ND2 do not
    parsed_dl = [parse_qs(urlparse(u).query) for u in out["downloads"]]
    assert [q["format"][0] for q in parsed_dl] == ["png", "jpg", "svg", "tiff", "nd2"]
    for q in parsed_dl[:3]:
        assert q["col"] == ["00FFFF,,FF00FF"]
    for q in parsed_dl[3:]:
        assert "col" not in q

    # Alt-solo assertions
    assert out["beforeSolo"] == [0, 1]
    assert out["soloed1"] == [1]
    assert out["ariaDuringSolo"] == ["false", "true", "false"]
    assert out["soloed2"] == [2]
    assert out["restoredFromSolo"] == [0, 1]


def test_50_step_undo_redo_stack_safe_astromap_clear_and_keyboard_shortcuts():
    script = r"""
const fs = require('fs'), path = require('path'), vm = require('vm');
const source = fs.readFileSync(path.join(process.argv[1], 'app.js'), 'utf8');
const ShortcutRouter = require(path.join(process.argv[1], 'shortcut-router-v1.js'));
const Astromap = require(path.join(process.argv[1], 'astromap-v1.js'));
const AnnotationStore = require(path.join(process.argv[1], 'annotation-store-v1.js'));

const nodes = new Map();
function el(id) {
  if (!nodes.has(id)) {
    const classes = new Set();
    nodes.set(id, {
      id, hidden: id === 'ann-editor' || id === 'shortcuts-modal', disabled: false,
      value: '', textContent: '', title: '', dataset: {}, style: {}, children: [],
      classList: {
        add: c => classes.add(c), remove: c => classes.delete(c),
        contains: c => classes.has(c),
        toggle(c, on) { const next = on === undefined ? !classes.has(c) : !!on; next ? classes.add(c) : classes.delete(c); },
      },
      setAttribute() {}, focus() {}, addEventListener() {},
      replaceChildren(...xs) { this.children = [...xs]; },
      querySelectorAll: () => [],
    });
  }
  return nodes.get(id);
}

const listeners = [];
const toasts = [];
const state = {
  info: { name: 'sample.ome.tif', width: 2000, height: 2000, storage: 'direct' },
  annotations: [], annContext: 0, annRevision: 0, annDirty: false, annReady: true,
  editingId: null, quitPreparing: false, undoStack: [], redoStack: [], historyApplying: false,
  astromap: { exporting: false, importReading: false, rows: new Map(), activeId: null },
  windows: { astromap: { isHidden: () => false } },
  viewportRelay: {},
};
const annotationStore = AnnotationStore.createStore({ state });
const ctx = vm.createContext({
  state, annotationStore, $, clamp: (v, lo, hi) => Math.max(lo, Math.min(hi, v)),
  window: {
    Nd2ShortcutRouter: ShortcutRouter, Nd2Astromap: Astromap,
    addEventListener(k, fn) { if (k === 'keydown') listeners.push(fn); },
  },
  document: {
    createElement: tag => ({ tag, type: '', className: '', textContent: '', onclick: null }),
  },
  JSON, Math, Number, String, Boolean, Array, Set, Object, Date,
  setTimeout: () => 1, clearTimeout() {},
  activeFrameContext: () => ({ key: '0' }),
  spatialImageReady: () => true,
  renderAnnotations() {}, rebuildAnnList() {}, refreshAstromapPanel() {},
  updateAnnotationRecoveryNotice() {}, scheduleAnnSave() {},
  showToast: msg => toasts.push(msg),
});
function $(id) { return el(id); }

const slice = (a, b) => source.slice(source.indexOf(a), source.indexOf(b, source.indexOf(a)));
vm.runInContext(
  slice('function annLocked(', 'function scopedButton(') +
  slice('function astromapReady(', 'function selectAstromapRoi(') +
  slice('function addAstromapRoi(', 'function refreshAstromapPanel(') +
  slice('/* ---- annotations: model', 'function normalizeAnnotationSite(') +
  slice('function openEditor(', 'function wireAnnotationPanel(') +
  slice('function setShortcutModalOpen(', 'function elementPoint(') +
  slice('let toastTimer = null;', 'function debounceToast('),
  ctx
);
ctx.selectAstromapRoi = id => { state.astromap.activeId = id; };
ctx.wireShortcutModal();
ctx.wireKeys();

function press(spec) {
  const ev = {
    key: '', code: '', metaKey: false, ctrlKey: false, shiftKey: false, altKey: false,
    repeat: false, defaultPrevented: false,
    target: { tagName: 'DIV', closest: () => null },
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() {},
    ...spec,
  };
  listeners.forEach(fn => fn(ev));
  return ev.defaultPrevented;
}

// 1. Create, move, edit, delete annotations + Undo/Redo
const pin = ctx.addAnnotation({ type: 'pin', x: 100, y: 200, text: 'initial' });
ctx.moveAnnotation(pin.id, 50, -30);
const movedPos = [state.annotations[0].x, state.annotations[0].y];

state.editingId = pin.id;
el('ann-editor').hidden = false;
el('ann-text').value = 'edited label';
ctx.closeEditor(true);
const editedText = state.annotations[0].text;

ctx.removeAnnotation(pin.id);
const afterDeleteCount = state.annotations.length;

// Cmd+Z undoes delete -> edit -> move -> create
press({ key: 'z', code: 'KeyZ', metaKey: true });
const undoDelete = JSON.parse(JSON.stringify(state.annotations));
press({ key: 'z', code: 'KeyZ', metaKey: true });
const undoEdit = state.annotations[0].text;
press({ key: 'z', code: 'KeyZ', metaKey: true });
const undoMove = [state.annotations[0].x, state.annotations[0].y];
press({ key: 'z', code: 'KeyZ', metaKey: true });
const undoCreateCount = state.annotations.length;

// Cmd+Shift+Z redoes create -> move
press({ key: 'Z', code: 'KeyZ', metaKey: true, shiftKey: true });
press({ key: 'Z', code: 'KeyZ', metaKey: true, shiftKey: true });
const redoMove = [state.annotations[0].x, state.annotations[0].y];

// Typing inside an INPUT does NOT trigger viewer undo or ? modal
const inputTarget = { tagName: 'INPUT', type: 'text', closest: () => ({}) };
const preventedInInputZ = press({ key: 'z', code: 'KeyZ', metaKey: true, target: inputTarget });
const preventedInInputQ = press({ key: '?', code: 'Slash', shiftKey: true, target: inputTarget });
const modalHiddenAfterInput = el('shortcuts-modal').hidden;

// ? opens modal, Esc closes modal
press({ key: '?', code: 'Slash', shiftKey: true });
const modalOpenOnQuestion = !el('shortcuts-modal').hidden;
press({ key: 'Escape', code: 'Escape' });
const modalClosedOnEsc = el('shortcuts-modal').hidden;

// 2. Astromap safe 2-step Clear all + toast Undo
ctx.addAstromapRoi({ x: 10, y: 10, w: 40, h: 40 });
ctx.addAstromapRoi({ x: 80, y: 80, w: 40, h: 40 });
const roisBeforeClear = ctx.astromapItems().length;
const firstClickCleared = ctx.requestClearAstromapRois(); // arms confirmation only!
const roisAfterFirstClick = ctx.astromapItems().length;
const confirmLabel = el('astromap-clear-all').textContent;
const secondClickCleared = ctx.requestClearAstromapRois(); // confirms & clears + shows Undo toast!
const roisAfterSecondClick = ctx.astromapItems().length;
// Click the inline Undo button inside #toast!
const toastUndoBtn = el('toast').children.find(c => c.className === 'toast-action');
toastUndoBtn.onclick({});
const roisAfterToastUndo = ctx.astromapItems().length;

// 3. Verify 50-step cap on undoStack
state.undoStack = [];
state.redoStack = [];
for (let i = 0; i < 58; i++) {
  ctx.addAnnotation({ type: 'pin', x: i * 5, y: i * 5, text: 'p' + i });
}
const cappedUndoLen = state.undoStack.length;

// 4. Verify Windows Ctrl+Z / Ctrl+Y classification in ShortcutRouter
const winUndo = ShortcutRouter.historyActionForEvent({ key: 'z', code: 'KeyZ', ctrlKey: true }, 'Win32');
const winRedoY = ShortcutRouter.historyActionForEvent({ key: 'y', code: 'KeyY', ctrlKey: true }, 'Win32');
const winRedoShiftZ = ShortcutRouter.historyActionForEvent({ key: 'Z', code: 'KeyZ', ctrlKey: true, shiftKey: true }, 'Win32');

process.stdout.write(JSON.stringify({
  movedPos, editedText, afterDeleteCount,
  undoDelete, undoEdit, undoMove, undoCreateCount, redoMove,
  preventedInInputZ, preventedInInputQ, modalHiddenAfterInput,
  modalOpenOnQuestion, modalClosedOnEsc,
  roisBeforeClear, firstClickCleared, roisAfterFirstClick, confirmLabel,
  secondClickCleared, roisAfterSecondClick, roisAfterToastUndo,
  cappedUndoLen, winUndo, winRedoY, winRedoShiftZ,
}));
"""
    out = json.loads(run_node(script, STATIC, check=True).stdout)
    assert out["movedPos"] == [150, 170]
    assert out["editedText"] == "edited label"
    assert out["afterDeleteCount"] == 0
    assert len(out["undoDelete"]) == 1 and out["undoDelete"][0]["text"] == "edited label"
    assert out["undoEdit"] == "initial"
    assert out["undoMove"] == [100, 200]
    assert out["undoCreateCount"] == 0
    assert out["redoMove"] == [150, 170]
    assert out["preventedInInputZ"] is False
    assert out["preventedInInputQ"] is False
    assert out["modalHiddenAfterInput"] is True
    assert out["modalOpenOnQuestion"] is True
    assert out["modalClosedOnEsc"] is True
    assert out["roisBeforeClear"] == 2
    assert out["firstClickCleared"] is False and out["roisAfterFirstClick"] == 2
    assert "Confirm clear (2)" in out["confirmLabel"]
    assert out["secondClickCleared"] is True and out["roisAfterSecondClick"] == 0
    assert out["roisAfterToastUndo"] == 2
    assert out["cappedUndoLen"] == 50
    assert (out["winUndo"], out["winRedoY"], out["winRedoShiftZ"]) == ("undo", "redo", "redo")


def test_wire_theme_preserves_explicit_light_or_dark_across_focus_and_tab_switches():
    script = r"""
const fs = require('fs'), path = require('path'), vm = require('vm');
const source = fs.readFileSync(path.join(process.argv[1], 'app.js'), 'utf8');
const storage = new Map();
const events = new Map();
const posted = [];
const uiPosts = [];
const docClasses = new Set();
const tbBtn = {
  title: '', dataset: {}, onclick: null,
  setAttribute() {}, querySelector: () => ({ innerHTML: '' }),
};
const ctx = vm.createContext({
  state: { info: { rgb: false }, lutWidgets: [] },
  document: {
    documentElement: {
      dataset: {},
      classList: {
        contains: c => docClasses.has(c),
        toggle(c, on) { on ? docClasses.add(c) : docClasses.delete(c); },
      },
    },
  },
  window: {
    parent: { postMessage: (msg) => posted.push(msg) },
    addEventListener(kind, fn) { if (!events.has(kind)) events.set(kind, []); events.get(kind).push(fn); },
  },
  location: { origin: 'http://127.0.0.1:9000', pathname: '/tok123/s/abcd1234/' },
  localStorage: { getItem: k => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, String(v)) },
  fetch: async (url, opts = {}) => {
    if (opts.method === 'POST') uiPosts.push({ url, body: JSON.parse(opts.body) });
    return { ok: false, status: 404 };
  },
  $: id => id === 'tb-theme' ? tbBtn : null,
  relayoutLuts() {},
});
const start = source.indexOf('const THEME_MODES =');
const end = source.indexOf('function inkColor(', start);
vm.runInContext(source.slice(start, end), ctx);
ctx.wireTheme();

const initialMode = ctx.document.documentElement.dataset.themeMode;
const initialTheme = ctx.currentTheme();

// User clicks Appearance once: auto -> light (on a fluorescence slide!)
tbBtn.onclick();
const chosenLight = { mode: ctx.document.documentElement.dataset.themeMode, theme: ctx.currentTheme() };

// Fire window focus AND shell theme-request message -> must stay light!
(events.get('focus') || []).forEach(fn => fn());
(events.get('message') || []).forEach(fn => fn({
  origin: 'http://127.0.0.1:9000', data: { nd2wsi: 'theme-request' },
}));
const afterFocusAndSwitch = { mode: ctx.document.documentElement.dataset.themeMode, theme: ctx.currentTheme() };

// User clicks Appearance again: light -> dark on an RGB brightfield slide!
ctx.state.info.rgb = true;
tbBtn.onclick();
(events.get('focus') || []).forEach(fn => fn());
(events.get('message') || []).forEach(fn => fn({
  origin: 'http://127.0.0.1:9000', data: { nd2wsi: 'theme-request' },
}));
const explicitDarkOnRgb = { mode: ctx.document.documentElement.dataset.themeMode, theme: ctx.currentTheme() };

process.stdout.write(JSON.stringify({
  initialMode, initialTheme, chosenLight, afterFocusAndSwitch, explicitDarkOnRgb, uiPosts,
}));
"""
    out = json.loads(run_node(script, STATIC, check=True).stdout)
    assert out["initialMode"] == "auto" and out["initialTheme"] == "dark"
    assert out["chosenLight"] == {"mode": "light", "theme": "light"}
    assert out["afterFocusAndSwitch"] == {"mode": "light", "theme": "light"}
    assert out["explicitDarkOnRgb"] == {"mode": "dark", "theme": "dark"}
    assert out["uiPosts"] == [
        {"url": "/tok123/api/ui-settings", "body": {"theme": "light"}},
        {"url": "/tok123/api/ui-settings", "body": {"theme": "dark"}},
    ]


def test_floating_windows_hydrates_and_saves_panels_via_ui_settings():
    script = r"""
const { createManager } = require(process.argv[1]);
function node() {
  const classes = new Set();
  return {
    style: {}, children: [], handlers: {}, clientWidth: 0, offsetHeight: 240, parts: {},
    classList: {
      add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c),
      toggle(c, on) { const v = on === undefined ? !classes.has(c) : !!on; v ? classes.add(c) : classes.delete(c); },
    },
    append(n) { this.children.push(n); },
    addEventListener(k, fn) { this.handlers[k] = fn; },
    setPointerCapture() {},
    querySelector(q) { return this.parts[q]; },
  };
}
const storageMap = new Map();
const posts = [];
const stage = { clientWidth: 1200, clientHeight: 800 };
const panels = [];
const manager = createManager({
  stage,
  document: { createElement: node, querySelectorAll: () => panels },
  storage: { getItem: k => storageMap.get(k) ?? null, setItem: (k, v) => storageMap.set(k, v) },
  uiSettingsUrl: '/tok/api/ui-settings',
  setTimeout: (fn) => { fn(); return 1; },
  clearTimeout() {},
  fetch: async (url, opts = {}) => {
    if (opts.method === 'POST') {
      posts.push({ url, body: JSON.parse(opts.body) });
      return { ok: true, json: async () => ({ ok: true }) };
    }
    return {
      ok: true,
      json: async () => ({
        ok: true,
        panels: { channels: { rect: { x: 180, y: 64, w: 320, h: 260 }, collapsed: true } },
      }),
    };
  },
});
const el = node();
panels.push(el);
for (const part of ['.win-body', '.win-titlebar', '.tl-close', '.tl-min', '.tl-zoom']) el.parts[part] = node();
manager.createWindow(el, {
  key: 'channels', def: () => ({ x: 14, y: 14, w: 256, h: null }),
  minW: 232, minH: 150, maxW: 600, zoomW: 480,
});
(async () => {
  await manager.hydrate();
  const hydrated = { left: el.style.left, top: el.style.top, width: el.style.width, collapsed: el.classList.contains('collapsed') };
  el.parts['.tl-min'].handlers.click(); // uncollapse -> persists to localStorage + POST /tok/api/ui-settings
  for (let i = 0; i < 5; i++) await Promise.resolve();
  process.stdout.write(JSON.stringify({ hydrated, posts }));
})();
"""
    out = json.loads(run_node(script, STATIC / "floating-windows-v1.js", check=True).stdout)
    assert out["hydrated"] == {"left": "180px", "top": "64px", "width": "320px", "collapsed": True}
    assert out["posts"] == [
        {
            "url": "/tok/api/ui-settings",
            "body": {"panels": {"channels": {"rect": {"x": 180, "y": 64, "w": 320, "h": 260}, "collapsed": False}}},
        }
    ]


def test_scene_picker_visibility_selection_shell_relay_and_standalone_navigation():
    doc = HtmlInspector((STATIC / "index.html").read_text(encoding="utf-8"))
    assert "scene-picker" in doc.ids
    assert "toolbar" in doc.ids["scene-picker"]["ancestors"]
    assert "hidden" in doc.ids["scene-picker"]["attrs"]

    script = r"""
const fs = require('fs'), path = require('path'), vm = require('vm');
const source = fs.readFileSync(path.join(process.argv[1], 'app.js'), 'utf8');
const hosted = process.argv[2] === 'hosted';

function makeEl(id = '') {
  const attrs = {};
  return {
    id, hidden: id === 'scene-picker' || id === 'scene-picker-menu' || id === 'ann-editor',
    disabled: false, textContent: '', title: '', className: '', dataset: {}, style: {}, children: [],
    setAttribute(k, v) { attrs[k] = String(v); },
    getAttribute(k) { return attrs[k] ?? null; },
    append(...xs) { this.children.push(...xs); },
    replaceChildren(...xs) { this.children = [...xs]; },
    getBoundingClientRect: () => ({ left: 120, bottom: 34 }),
  };
}
const nodes = new Map(['scene-picker', 'scene-picker-btn', 'scene-picker-menu', 'ann-editor'].map(id => [id, makeEl(id)]));
const posted = [], replaced = [], selectCalls = [];
const state = {
  quitPreparing: false, annDirty: false, lutPersistence: null,
  info: {
    name: 'organoids.lif', seriesCount: 1, seriesIndex: 0, seriesList: [],
  },
};
const win = {
  innerWidth: 1280,
  location: { replace: url => replaced.push(url) },
  addEventListener() {},
};
win.parent = hosted ? { postMessage: (msg, origin) => posted.push({ msg, origin }) } : win;

const ctx = vm.createContext({
  state, window: win, $: id => nodes.get(id),
  location: { origin: 'http://127.0.0.1:8123', pathname: '/sec_tok/s/11223344/' },
  document: { createElement: () => makeEl(), addEventListener() {} },
  fetch: async (url, opts) => {
    selectCalls.push({ url, body: JSON.parse(opts.body) });
    return {
      ok: true,
      json: async () => ({ ok: true, sid: '99aabbcc', name: 'organoids.lif · Scene 2', url: '/s/99aabbcc/' }),
    };
  },
  VIEWPORT_PROTOCOL_VERSION: 2,
  currentSlideSid: () => '11223344',
  fmtInt: n => Number(n).toLocaleString('en-US'),
  closeEditor() {}, showToast() {}, Promise, Number, Array, String, Boolean, Math, encodeURIComponent,
});
const start = source.indexOf('function resolveSceneUrl(');
const end = source.indexOf('function kindMark(', start);
vm.runInContext(source.slice(start, end), ctx);

(async () => {
  ctx.wireScenePicker();
  const singleHidden = nodes.get('scene-picker').hidden;

  state.info = {
    name: 'organoids.lif',
    seriesCount: 3,
    seriesIndex: 0,
    seriesList: [
      { index: 0, name: 'Overview', width: 2048, height: 2048, channels: 3, zCount: 1, tCount: 1 },
      { index: 1, name: 'Crypt Z-Stack', width: 1024, height: 1024, channels: 4, zCount: 25, tCount: 1 },
      { index: 2, name: 'Timecourse', width: 512, height: 512, channels: 2, zCount: 5, tCount: 12 },
    ],
  };
  ctx.renderScenePicker(state.info);
  const label = nodes.get('scene-picker-btn').textContent;
  const optionRows = nodes.get('scene-picker-menu').children.map(item => ({
    name: item.children[0].textContent,
    meta: item.children[1].textContent,
    selected: item.getAttribute('aria-selected'),
  }));
  await nodes.get('scene-picker-menu').children[1].onclick();

  process.stdout.write(JSON.stringify({
    singleHidden, multiHidden: nodes.get('scene-picker').hidden,
    label, optionRows, selectCalls, posted, replaced,
  }));
})();
"""
    hosted_out = json.loads(run_node(script, STATIC, "hosted", check=True).stdout)
    assert hosted_out["singleHidden"] is True
    assert hosted_out["multiHidden"] is False
    assert hosted_out["label"] == "Series 1 / 3 ▾"
    assert hosted_out["optionRows"] == [
        {"name": "1. Overview", "meta": "2,048×2,048 · 3 ch", "selected": "true"},
        {"name": "2. Crypt Z-Stack", "meta": "1,024×1,024 · 4 ch · Z 25", "selected": "false"},
        {"name": "3. Timecourse", "meta": "512×512 · 2 ch · Z 5 · T 12", "selected": "false"},
    ]
    assert hosted_out["selectCalls"] == [{"url": "api/select-scene", "body": {"series": 1}}]
    assert hosted_out["posted"] == [
        {
            "origin": "http://127.0.0.1:8123",
            "msg": {
                "nd2wsi": "scene-selected",
                "version": 2,
                "oldSid": "11223344",
                "sid": "99aabbcc",
                "name": "organoids.lif · Scene 2",
                "url": "/sec_tok/s/99aabbcc/",
            },
        }
    ]
    assert hosted_out["replaced"] == []

    standalone_out = json.loads(run_node(script, STATIC, "standalone", check=True).stdout)
    assert standalone_out["posted"] == []
    assert standalone_out["replaced"] == ["/sec_tok/s/99aabbcc/"]

    # Also verify shell-v1.js receiveSceneSelected cleanly retargets the open tab in place
    shell_retarget = r"""
const fs = require('fs'), path = require('path'), vm = require('vm');
const shellSrc = fs.readFileSync(path.join(process.argv[1], 'shell-v1.js'), 'utf8');
const start = shellSrc.indexOf('function receiveSceneSelected(');
const end = shellSrc.indexOf('\n}', start) + 2;
const closedSids = [];
const activated = [];
let slides = [
  { sid: 'first001', name: 'first.nd2' },
  { sid: '11223344', name: 'organoids.lif · Scene 1' },
  { sid: 'third003', name: 'third.svs' },
];
const ctx = vm.createContext({
  quitPreparation: null,
  slides,
  frames: new Map(),
  render() {},
  activate: sid => activated.push(sid),
  refresh: async (selectSid, beforeActivate) => {
    slides.push({ sid: '99aabbcc', name: 'organoids.lif · Scene 2' });
    slides = slides.filter(s => !closedSids.includes(s.sid));
    ctx.slides = slides;
    if (beforeActivate) beforeActivate();
    activated.push(selectSid);
  },
  fetch: async (url, opts = {}) => {
    if (url === 'api/slides') {
      return {
        ok: true,
        json: async () => ({
          slides: [...slides, { sid: '99aabbcc', name: 'organoids.lif · Scene 2' }],
        }),
      };
    }
    if (url === 'api/close' && opts.method === 'POST') {
      closedSids.push(JSON.parse(opts.body).sid);
      return { ok: true, json: async () => ({ ok: true }) };
    }
    return { ok: false };
  },
});
vm.runInContext(shellSrc.slice(start, end), ctx);
(async () => {
  ctx.receiveSceneSelected('11223344', { sid: '99aabbcc', name: 'organoids.lif · Scene 2' });
  for (let i = 0; i < 10; i++) await Promise.resolve();
  process.stdout.write(JSON.stringify({ closedSids, activated, slides: ctx.slides }));
})();
"""
    retarget_out = json.loads(run_node(shell_retarget, STATIC, check=True).stdout)
    assert retarget_out["closedSids"] == ["11223344"]
    assert retarget_out["activated"] == ["99aabbcc"]
    assert [s["sid"] for s in retarget_out["slides"]] == ["first001", "99aabbcc", "third003"]


def test_roi_nd2_dtype_gating_promotes_tiff_and_annotation_buttons_and_stems():
    doc = HtmlInspector((STATIC / "index.html").read_text(encoding="utf-8"))
    assert {"btn", "primary", "full"} <= doc.ids["ann-geojson"]["classes"]
    assert "primary" not in doc.ids["ann-export"]["classes"]
    assert doc.text_of("ann-open") == "Import…"
    assert doc.text_of("ann-export") == "Export .json"
    assert doc.text_of("ann-geojson") == "Export GeoJSON"

    script = r"""
const fs = require('fs'), path = require('path'), vm = require('vm');
const source = fs.readFileSync(path.join(process.argv[1], 'app.js'), 'utf8');

function makeBtn(id, initialPrimary = false) {
  const classes = new Set(['btn', ...(initialPrimary ? ['primary'] : [])]);
  return {
    id, disabled: false, title: id + '-default', textContent: '', dataset: {},
    classList: {
      add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c),
      toggle(c, on) { on ? classes.add(c) : classes.delete(c); },
    },
  };
}
const els = new Map([
  ['roi-dl-nd2', makeBtn('roi-dl-nd2', true)],
  ['roi-dl-tiff', makeBtn('roi-dl-tiff', false)],
]);
const getEl = id => { if (!els.has(id)) els.set(id, makeBtn(id)); return els.get(id); };

const downloads = [];
const state = {
  info: {
    name: 'kidney_scan.ome.tif',
    nd2Export: false,
    nd2ExportReason: 'ND2 export supports uint8 and uint16 images only (this source is float32)',
    selection: { t: 0, p: 2, z: 4 },
  },
  plate: null, roi: { x: 0, y: 0, w: 100, h: 100 }, annReady: true, annotations: [],
};
const ctx = vm.createContext({
  state, $: getEl, Boolean, String, Number, Array, Object,
  activeFrameContext: () => ({ key: '0', frame: null }),
  frameOwnsRoi: () => true,
  annotationOwnerReady: () => true,
  annLocked: () => false,
  refreshAstromapPanel() {},
  syncScaleBarExportControls() {},
  basename: p => String(p || '').split(/[\\/]/).pop(),
  pixelSize: () => [0.5, 0.5],
  closeEditor() {},
  showToast() {},
  downloadJsonFile: (_doc, filename) => downloads.push(filename),
});
const slice = (a, b) => source.slice(source.indexOf(a), source.indexOf(b, source.indexOf(a)));
vm.runInContext(
  slice('function scopedButton(', 'function setTool(') +
  slice('function safeFilenamePart(', 'function rebuildAnnList('),
  ctx
);
getEl('ann-editor').hidden = true;

ctx.syncFrameScopedControls();
const gated = {
  nd2Disabled: getEl('roi-dl-nd2').disabled,
  nd2Title: getEl('roi-dl-nd2').title,
  nd2Primary: getEl('roi-dl-nd2').classList.contains('primary'),
  tiffPrimary: getEl('roi-dl-tiff').classList.contains('primary'),
};

state.info.nd2Export = true;
state.info.nd2ExportReason = '';
ctx.syncFrameScopedControls();
const ungated = {
  nd2Disabled: getEl('roi-dl-nd2').disabled,
  nd2Primary: getEl('roi-dl-nd2').classList.contains('primary'),
  tiffPrimary: getEl('roi-dl-tiff').classList.contains('primary'),
};

const stems = {};
for (const name of ['a.ome.tif', 'b.ome.tiff', 'c.tif', 'd.tiff', 'e.sdpc', 'f.nd2svs', 'g.jpg', 'h.jpeg', 'i.png']) {
  state.info.name = name;
  downloads.length = 0;
  ctx.exportAnnotationsJSON();
  stems[name] = { json: downloads[0], geojson: ctx.geojsonFilename(state.info) };
}

process.stdout.write(JSON.stringify({ gated, ungated, stems }));
"""
    out = json.loads(run_node(script, STATIC, check=True).stdout)
    assert out["gated"] == {
        "nd2Disabled": True,
        "nd2Title": "ND2 export supports uint8 and uint16 images only (this source is float32)",
        "nd2Primary": False,
        "tiffPrimary": True,
    }
    assert out["ungated"] == {
        "nd2Disabled": False,
        "nd2Primary": True,
        "tiffPrimary": False,
    }
    for filename, expected_stem in [
        ("a.ome.tif", "a"),
        ("b.ome.tiff", "b"),
        ("c.tif", "c"),
        ("d.tiff", "d"),
        ("e.sdpc", "e"),
        ("f.nd2svs", "f"),
        ("g.jpg", "g"),
        ("h.jpeg", "h"),
        ("i.png", "i"),
    ]:
        assert out["stems"][filename]["json"] == f"{expected_stem}.annotations.json"
        assert out["stems"][filename]["geojson"] == f"{expected_stem}--t0-p2-z4.geojson"
