"""Regression tests for macOS native QA findings (v2.6.7)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from node_host import run_node

from nd2wsi.translucent_smoke import pane_eval

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "nd2wsi" / "static"


def test_channel_swatch_buttons_do_not_pollute_state_info_json():
    """buildChannelPanel must not attach DOM nodes onto state.info.channels[i]."""
    script = r"""
const fs = require('fs'), path = require('path'), vm = require('vm');
const source = fs.readFileSync(path.join(process.argv[1], 'app.js'), 'utf8');
const L = require(path.join(process.argv[1], 'lut-controls-v1.js'));
const transfer = require(path.join(process.argv[1], 'lut-transfer-v1.js'));
const ctx2d = new Proxy({}, { get: (o, k) => o[k] || (() => {}) });
const nodes = [];
function makeNode(tag) {
  const n = {
    tag, children: [], handlers: {}, attrs: {}, style: {}, disabled: false,
    textContent: '', hidden: false,
    append(...xs) { this.children.push(...xs); },
    setAttribute(k, v) { this.attrs[k] = v; },
    replaceChildren(...xs) { this.children = [...xs]; },
    focus() {}, showModal() {}, close() {},
    addEventListener(k, f) { this.handlers[k] = f; },
    removeEventListener(k, f) { if (this.handlers[k] === f) delete this.handlers[k]; },
    getContext: () => ctx2d, setPointerCapture() {},
    getBoundingClientRect: () => ({ left: 0, top: 0 }),
  };
  // Simulate cross-realm cyclic DOM link (e.g., parentNode/ownerDocument) that hangs pywebview.stringify
  n.ownerDocument = { body: n };
  n.selfCycle = n;
  n.classList = {
    contains: c => (n.className || '').split(' ').includes(c),
    toggle(c, on) {
      const s = new Set((n.className || '').split(' '));
      on ? s.add(c) : s.delete(c);
      n.className = [...s].join(' ');
    },
  };
  nodes.push(n);
  return n;
}
const controls = { 'channel-list': makeNode('div'), 'lut-auto-range': makeNode('input'), 'tb-channels': makeNode('button') };
for (const id of ['lut-type', 'lut-copy', 'lut-paste', 'lut-cancel']) controls[id] = makeNode('button');
controls['lut-dialog'] = makeNode('dialog');
controls['lut-form'] = makeNode('form');
controls['lut-fields'] = makeNode('div');
controls['lut-error'] = makeNode('p');
for (const action of ['type', 'copy', 'paste']) controls['lut-' + action + '-tooltip'] = makeNode('div');

const channels = [
  { label: 'DAPI', color: '0000FF', window: { min: 0, max: 4095, start: 0, end: 4095 } },
  { label: 'FITC', color: '00FF00', window: { min: 0, max: 4095, start: 0, end: 4095 } },
];
const state = {
  info: { rgb: false, dtype: 'uint16', channels },
  channels: [0, 1], luts: [null, null], lutWidgets: [], lutAutoRange: false,
  histogram: { failed: false },
  windows: { channels: { bodyWidth: () => 240, close() {} } },
};
const win = { Nd2LutControls: L, Nd2LutTransfer: transfer, devicePixelRatio: 1, addEventListener() {}, removeEventListener() {} };
const c = {
  state, window: win, URLSearchParams,
  document: { createElement: makeNode, addEventListener() {}, removeEventListener() {} },
  $: id => controls[id], VIEWPORT_PROTOCOL_VERSION: 1, inkColor: () => '', currentTheme: () => 'dark',
  fmtInt: String, loadHistograms() {}, refreshTiles() {},
  applyLuts: Object.assign(() => {}, { flush() {} }),
};
vm.createContext(c);
vm.runInContext(source.slice(source.indexOf('function macSwitch('), source.indexOf('/* ---- per-channel LUT')), c);
vm.runInContext(source.slice(source.indexOf('function buildLutRow('), source.indexOf('function relayoutLuts(')), c);
c.buildChannelPanel();

// 1. state.info must remain strictly JSON-serializable (no cyclic DOM node on channels[i])
const serialized = JSON.stringify(state.info);
const hasSwatchProp = state.info.channels.some(ch => '_swatchBtn' in ch);

// 2. setChannelColor without explicit swatchEl must still update the rendered swatch button via WeakMap
const changed = c.setChannelColor(0, 'FF00FF');
const swatches = nodes.filter(n => n.tag === 'button' && n.className === 'swatch');
process.stdout.write(JSON.stringify({
  hasSwatchProp,
  serializedOk: serialized.includes('"DAPI"'),
  changed,
  swatchBg: swatches[0]?.style?.background || null,
}));
"""
    result = run_node(script, STATIC, check=True)
    data = json.loads(result.stdout)
    assert data["hasSwatchProp"] is False
    assert data["serializedOk"] is True
    assert data["changed"] is True
    assert data["swatchBg"] == "#FF00FF"


def test_pane_eval_exposes_pixel_size_and_astromap_items():
    """pane_eval must bind pixelSize and astromapItems from __win for QA scripts."""
    parent = MagicMock()
    parent.evaluate_js.return_value = 1
    assert pane_eval(parent, "astromapItems().length") == 1
    js = parent.evaluate_js.call_args.args[0]
    assert "const pixelSize = __win.pixelSize;" in js
    assert "const astromapItems = __win.astromapItems;" in js


def test_native_qa_scripts_mac_qa_contracts():
    """Verify Mac-local native QA scripts reflect current glass tokens, __win scoping, and isolated caches."""
    final_beta = (ROOT / "qa" / "check_native_final_beta.py").read_text(encoding="utf-8")
    assert "__win.__qaFetch=__win.fetch.bind(__win)" in final_beta
    assert "__win.HTMLAnchorElement.prototype.click" in final_beta
    assert "rgba(255, 255, 255, 0.82)" in final_beta
    assert "blur(26px)" in final_beta
    assert "rgba(25, 25, 25, 0.78)" in final_beta

    translucent = (ROOT / "qa" / "check_native_translucent.py").read_text(encoding="utf-8")
    assert 'initial["width"] >= 900' in translucent
    assert "zoomControl" in translucent

    for name in ("check_native_astromap.py", "check_native_tracing_keys.py"):
        text = (ROOT / "qa" / name).read_text(encoding="utf-8")
        assert "ensure_cache" in text
        assert "isolated-input" in text


def test_extracted_js_controllers_expose_window_globals():
    """Extracted JS controllers must attach their helpers onto window for cross-frame callers."""
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "Object.assign(window, slideInspector);" in app_js
    assert "loadSlideInspector" in app_js
    assert "layoutPlate" in app_js
    assert "Object.assign(window, {" in app_js

    shell_js = (STATIC / "shell-v1.js").read_text(encoding="utf-8")
    assert "Object.assign(window, compareEngine);" in shell_js

