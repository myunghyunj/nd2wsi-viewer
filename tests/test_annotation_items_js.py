"""Annotation items from files and sidecars, and the label editor.

Items another tool wrote must never break rendering or reach a later
autosave; a label typed in the editor must survive switching marks. The
production store (``annotation-store-v1.js``) is loaded with ``require``;
the editor and color helpers are sliced out of ``app.js`` into a fake DOM.
"""

import json
from pathlib import Path

from node_host import run_node

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi" / "static"


def _run(script):
    result = run_node(
        "(async () => {" + script + "})().catch((error) => { console.error(error); process.exit(1); });",
        str(STATIC), check=True,
    )
    return json.loads(result.stdout)


STORE = r"""
const path = require("path");
const { createStore, normalizeItems } = require(path.join(process.argv[1], "annotation-store-v1.js"));
const emit = (value) => process.stdout.write(JSON.stringify(value));
const tick = async (turns = 30) => { for (let i = 0; i < turns; i++) await Promise.resolve(); };
"""


def test_normalize_repairs_color_text_and_ids_and_keeps_sound_items_as_they_are():
    out = _run(STORE + r"""
let n = 0;
const sound = {id: "keep", type: "pin", x: 1, y: 2, text: "ok", color: "#30d158"};
const items = normalizeItems([
  sound,
  {type: "box", x: 0, y: 0, w: 5, h: 5, color: 16711680, text: 7},
  {id: "keep", type: "line", x1: 0, y1: 0, x2: 3, y2: 4},
  {type: "geojson", geojson: {geometry: {type: "Polygon", coordinates: [[[0, 0], [4, 0], [4, 4], [0, 0]]]}}},
], () => "new" + n++);
emit({same: items[0] === sound, ids: items.map((a) => a.id), color: "color" in items[1], text: items[1].text});
""")
    assert out["same"] is True
    assert out["ids"] == ["keep", "new0", "new1", "new2"]
    assert out["color"] is False and out["text"] == "7"


def test_normalize_refuses_items_the_pane_cannot_draw():
    out = _run(STORE + r"""
const refused = [
  [null], [[1, 2]], [{type: "circle", x: 1, y: 1}], [{type: "pin", x: "1", y: 2}],
  [{type: "box", x: 0, y: 0, w: Infinity, h: 1}], [{type: "line", x1: 0, y1: 0, x2: 1}],
  [{type: "geojson"}], [{type: "geojson", geojson: {geometry: {type: "Polygon", coordinates: [[0, 0]]}}}],
  [{type: "toString", x: 1, y: 1}],
].map((items) => { try { normalizeItems(items, () => "x"); return null; } catch (error) { return error.message; } });
emit(refused);
""")
    assert all(message and message.startswith("annotation 1: ") for message in out), out


def test_a_sidecar_the_pane_cannot_draw_stays_unloaded_and_is_never_saved_over():
    out = _run(STORE + r"""
const state = {plate: null, info: {notes: []}, annotations: [], annSite: null, annReady: false,
  annDirty: false, quitPreparing: false, editingId: null};
const requests = [], statuses = [], timers = [];
const store = createStore({
  state,
  fetch: (url, init = {}) => {
    requests.push(init.method || "GET");
    return Promise.resolve({ok: true, json: async () => ({
      items: [{id: "a", type: "pin", x: 1, y: 2}, {id: "b", type: "box", x: 0, y: 0, w: "wide", h: 2}],
      path: "/sidecars/slide.json", revision: "r1"})});
  },
  onStatus: (message) => statuses.push(message),
  setTimer: (fn) => { timers.push(fn); return timers.length; }, clearTimer: () => {},
});
store.load(null); await tick();
const status = statuses.at(-1);
// every path that could send the pane's (empty) list for this owner
store.markDirty(); store.scheduleSave();
for (const fire of timers.splice(0)) fire();
await store.retryFailed(); await store.flushOnPagehide(); await store.save(); await tick();
let flushed = "ok";
try { await store.flushForUpdate(); } catch (error) { flushed = error.message; }
emit({ready: state.annReady, dirty: state.annDirty, items: state.annotations.length, requests, status, flushed});
""")
    assert out["ready"] is False and out["dirty"] is False and out["items"] == 0
    assert out["requests"] == ["GET"]
    assert out["status"].startswith("Could not load annotations for this site (annotation 2: ")
    # nothing unsaved exists, so an update or close is not held up by it
    assert out["flushed"] == "ok"


APP = r"""
const fs = require("fs"), vm = require("vm"), path = require("path");
const source = fs.readFileSync(path.join(process.argv[1], "app.js"), "utf8").replace(/\r\n/g, "\n");
const production = (name) => {
  const start = source.indexOf("function " + name + "("), end = source.indexOf("\n}\n", start);
  if (start < 0 || end < 0) throw new Error("production source not found: " + name);
  return source.slice(start, end) + "\n}\n";
};
const elements = new Map();
const $ = (id) => {
  if (!elements.has(id)) elements.set(id, {hidden: true, value: "", style: {}, focused: 0, focus() { this.focused++; }});
  return elements.get(id);
};
$("stage-wrap").clientWidth = 800; $("stage-wrap").clientHeight = 600;
const state = {annotations: [], editingId: null};
const changes = [];
const context = {state, $, Math, clamp: (v, lo, hi) => Math.min(hi, Math.max(lo, v)),
  toEl: (x, y) => ({x, y}), annotationsChanged: () => changes.push(JSON.parse(JSON.stringify(state.annotations)))};
vm.createContext(context);
vm.runInContext('const ACCENT = "#0a84ff";\n' + ["openEditor", "closeEditor", "annColor", "hexAlpha"].map(production).join("\n") +
  "\nthis.api = {openEditor, closeEditor, annColor, hexAlpha};", context);
const api = context.api;
const emit = (value) => process.stdout.write(JSON.stringify(value));
"""


def test_switching_marks_or_placing_a_new_one_keeps_the_typed_label():
    out = _run(APP + r"""
state.annotations = [{id: "a", type: "pin", x: 10, y: 10, text: ""}, {id: "b", type: "pin", x: 50, y: 50, text: "old"}];
api.openEditor("a"); $("ann-text").value = "nucleus 1 ";
api.openEditor("b");
const afterSwitch = {a: state.annotations[0].text, editing: state.editingId, field: $("ann-text").value, saves: changes.length};
$("ann-text").value = "typed";
api.openEditor("b");
const reopened = {field: $("ann-text").value, saves: changes.length};
state.annotations.push({id: "c", type: "box", x: 0, y: 0, w: 4, h: 4});
api.openEditor("c");
emit({afterSwitch, reopened, b: state.annotations[1].text, finalSaves: changes.length});
""")
    assert out["afterSwitch"] == {"a": "nucleus 1", "editing": "b", "field": "old", "saves": 1}
    assert out["reopened"] == {"field": "typed", "saves": 1}
    assert out["b"] == "typed" and out["finalSaves"] == 2


def test_closing_an_unchanged_label_schedules_no_save():
    out = _run(APP + r"""
state.annotations = [{id: "a", type: "pin", x: 1, y: 1, text: "same"}];
api.openEditor("a"); api.closeEditor(true);
emit({saves: changes.length, hidden: $("ann-editor").hidden});
""")
    assert out == {"saves": 0, "hidden": True}


def test_update_flush_drops_a_drag_in_progress_before_freezing_edits():
    # A pin or box released after editing froze would be added unsaved.
    out = _run(r"""
const fs = require("fs"), vm = require("vm"), path = require("path");
const source = fs.readFileSync(path.join(process.argv[1], "app.js"), "utf8").replace(/\r\n/g, "\n");
const start = source.indexOf("async function flushAnnotationsForUpdate("), end = source.indexOf("\n}\n", start);
const order = [];
const state = {quitPreparing: false, cancelToolDrag: () => order.push(["cancel", state.quitPreparing])};
const context = {state, $: () => ({hidden: true}), closeEditor: () => order.push(["editor"]),
  annotationStore: {flushForUpdate: async () => order.push(["flush", state.quitPreparing])}};
vm.createContext(context);
vm.runInContext(source.slice(start, end) + "\n}\nthis.flush = flushAnnotationsForUpdate;", context);
await context.flush();
process.stdout.write(JSON.stringify(order));
""")
    assert out == [["cancel", False], ["flush", True]]


def test_a_color_that_is_not_a_hex_string_falls_back_instead_of_throwing():
    out = _run(APP + r"""
emit([16711680, "red", null, [255, 0, 0], "#ff453a"].map((color) => api.hexAlpha(api.annColor({color}), 0.5)));
""")
    assert out[:4] == ["rgba(10,132,255,0.5)"] * 4
    assert out[4] == "rgba(255,69,58,0.5)"
