"""Closed annotation windows skip their per-item rows and catch up when they open.

The Annotations list and the Astromap region rows run from production app.js
in a small fake DOM. While a window is hidden, edits and plate frames leave its
rows alone; opening it refreshes them, and a focused note keeps its node.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi" / "static"
APP = STATIC / "app.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SETUP = r"""
const fs = require('fs'), vm = require('vm'), path = require('path');
const STATIC = process.argv[1];
const source = fs.readFileSync(path.join(STATIC, 'app.js'), 'utf8').replace(/\r\n/g, '\n');
const production = name => {
  const i = source.indexOf('function ' + name + '('), j = source.indexOf('\n}\n', i);
  if (i < 0 || j < 0) throw new Error('production source not found: ' + name);
  return source.slice(i, j) + '\n}\n';
};
let created = 0;
class Node {
  constructor(tag) {
    this.tag = tag; this.children = []; this.parentNode = null; this.attrs = {}; this.style = {};
    this.classes = new Set(); this.textContent = ''; this.value = '';
    this.classList = {toggle: (k, on) => (on ? this.classes.add(k) : this.classes.delete(k)),
      add: k => this.classes.add(k), remove: k => this.classes.delete(k)};
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  append(...nodes) { for (const n of nodes) { n.remove(); n.parentNode = this; this.children.push(n); } }
  insertBefore(n, ref) {
    n.remove(); n.parentNode = this;
    const i = this.children.indexOf(ref);
    this.children.splice(i < 0 ? this.children.length : i, 0, n);
  }
  remove() {
    if (!this.parentNode) return;
    this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1);
    this.parentNode = null;
  }
  replaceChildren(...nodes) { for (const c of [...this.children]) c.remove(); this.append(...nodes); }
}
const nodes = new Map();
const $ = id => { if (!nodes.has(id)) nodes.set(id, new Node('div')); return nodes.get(id); };
const windows = {annot: {hidden: true}, astromap: {hidden: true}};
const posted = [];
const state = {
  info: {width: 8000, height: 4000}, annotations: [], quitPreparing: false,
  astromap: {rows: new Map(), activeId: null, exporting: false},
  windows: {
    annot: {isHidden: () => windows.annot.hidden},
    astromap: {isHidden: () => windows.astromap.hidden},
  },
};
const context = vm.createContext({
  state, $, console,
  window: {Nd2Astromap: require(path.join(STATIC, 'astromap-v1.js')),
    Nd2AnnotationGeoJSON: require(path.join(STATIC, 'annotation-geojson-v1.js'))},
  document: {activeElement: null, createElement(tag) { created++; return new Node(tag); }},
  fmtInt: v => String(Math.round(v)), annLocked: () => false, activeFrameContext: () => ({key: 'k'}),
  annotationOwnerReady: () => true, spatialImageReady: () => true, frameOwnsRoi: () => false,
  refreshAstromapImport() {}, postAstromapState: () => posted.push(windows.astromap.hidden),
  annColor: a => a.color || '#0a84ff', lineLengthLabel: () => '1 px',
  cycleAnnColor() {}, removeAnnotation() {}, flyToAnnotation() {}, annotationsChanged() {}, selectAstromapRoi() {},
});
vm.runInContext(['astromapReady', 'astromapItems', 'astromapDisplayNote', 'refreshAstromapPanel', 'rebuildAnnList']
  .map(production).join('\n'), context);
const c = context;
const regions = n => Array.from({length: n}, (_, i) => ({id: 'r' + i, type: 'box', astromap: true,
  x: 10 * i, y: 5 * i, w: 40, h: 30, text: 'note ' + i}));
"""


def run(body):
    script = SETUP + "\nconst result = (() => {\n" + body + "\n})();\nprocess.stdout.write(JSON.stringify(result));"
    result = subprocess.run([NODE, "-e", script, str(STATIC)], capture_output=True, text=True,
                            encoding="utf-8", check=True, timeout=30)
    return json.loads(result.stdout)


def test_a_closed_annotation_list_is_left_alone_and_rebuilt_when_it_opens():
    out = run(r"""
      state.annotations = [...regions(3), {id: 'pin', type: 'pin', x: 1, y: 2, text: ''}];
      created = 0;
      c.rebuildAnnList();
      const hidden = {created, rows: $('ann-list').children.length, empty: $('ann-empty').style.display ?? null};
      windows.annot.hidden = false;
      c.rebuildAnnList();
      const shown = {created, rows: $('ann-list').children.map(row => row.children[1].textContent)};
      state.annotations.pop(); windows.annot.hidden = true;
      c.rebuildAnnList();
      const stale = $('ann-list').children.length;
      windows.annot.hidden = false; c.rebuildAnnList();
      return {hidden, shown, stale, reopened: $('ann-list').children.length};
    """)
    assert out["hidden"] == {"created": 0, "rows": 0, "empty": None}
    assert out["shown"] == {"created": 4 * 5, "rows": ["note 0", "note 1", "note 2", "Pin"]}
    # a closed list may show stale rows; nobody sees them, and opening replaces them
    assert out["stale"] == 4 and out["reopened"] == 3


def test_closed_astromap_rows_wait_for_the_panel_while_its_state_stays_current():
    out = run(r"""
      state.annotations = regions(3); state.astromap.activeId = 'r1';
      created = 0;
      c.refreshAstromapPanel();
      const hidden = {created, rows: state.astromap.rows.size, list: $('astromap-list').children.length,
        empty: $('astromap-empty').hidden, exportDisabled: $('astromap-export').disabled, posted: posted.slice()};
      windows.astromap.hidden = false;
      c.refreshAstromapPanel();
      const shown = {created, numbers: $('astromap-list').children.map(row => row.children[1].textContent),
        selected: $('astromap-list').children.map(row => row.classes.has('selected'))};
      // while closed again: an edit removes one region and renames another
      windows.astromap.hidden = true;
      const keptRows = [...state.astromap.rows.values()];
      state.annotations = state.annotations.filter(a => a.id !== 'r0');
      state.annotations[0].text = 'renamed';
      created = 0;
      c.refreshAstromapPanel();
      const untouched = {created, rows: state.astromap.rows.size, note: keptRows[1].note.value,
        active: state.astromap.activeId};
      windows.astromap.hidden = false;
      c.refreshAstromapPanel();
      return {hidden, shown, untouched, reopened: {created,
        numbers: $('astromap-list').children.map(row => row.children[1].textContent),
        notes: $('astromap-list').children.map(row => row.children[2].value),
        reused: $('astromap-list').children[0] === keptRows[1].el}};
    """)
    assert out["hidden"] == {"created": 0, "rows": 0, "list": 0, "empty": True,
                             "exportDisabled": False, "posted": [True]}
    assert out["shown"] == {"created": 3 * 6, "numbers": ["1", "2", "3"], "selected": [False, True, False]}
    assert out["untouched"] == {"created": 0, "rows": 3, "note": "note 1", "active": "r1"}
    assert out["reopened"] == {"created": 0, "numbers": ["1", "2"], "notes": ["renamed", "note 2"], "reused": True}


def test_a_focused_note_keeps_its_node_and_text_while_rows_refresh():
    out = run(r"""
      windows.astromap.hidden = false;
      state.annotations = regions(3);
      c.refreshAstromapPanel();
      const row = state.astromap.rows.get('r1');
      context.document.activeElement = row.note;
      row.note.value = 'typing a note';
      state.annotations.unshift({id: 'new', type: 'box', astromap: true, x: 1, y: 1, w: 2, h: 2, text: ''});
      c.refreshAstromapPanel();
      return {same: state.astromap.rows.get('r1').note === row.note, value: row.note.value,
        attached: row.el.parentNode === $('astromap-list'),
        order: $('astromap-list').children.map(el => el.children[1].textContent)};
    """)
    assert out == {"same": True, "value": "typing a note", "attached": True, "order": ["1", "2", "3", "4"]}


def test_both_windows_refresh_their_rows_when_they_open():
    app = APP.read_text(encoding="utf-8").replace("\r\n", "\n")
    windows = app[app.index("  state.windows = {"):app.index("function makeMacWindow(")]
    annot = windows[windows.index("annot: makeMacWindow("):windows.index("astromap: makeMacWindow(")]
    astromap = windows[windows.index("astromap: makeMacWindow("):windows.index("info: makeMacWindow(")]
    assert re.search(r"onOpen: \(\) => rebuildAnnList\(\),", annot)
    assert re.search(r"onOpen: \(\) => refreshAstromapPanel\(\),", astromap)
