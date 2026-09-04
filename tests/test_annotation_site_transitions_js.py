"""Production annotation routing and site-transition persistence tests."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "nd2wsi" / "static" / "app.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _run(body):
    script = r"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const section = (start, end) => {
  const a = source.indexOf(start);
  const b = source.indexOf(end, a);
  if (a < 0 || b < 0) throw new Error("production function section not found: " + start);
  return source.slice(a, b);
};
const editor = {hidden: false};
const state = {
  plate: {focus: null},
  info: {plate: {P: 2}, notes: []},
  annotations: [],
  annPath: null,
  annSite: null,
  annReady: false,
  annDirty: false,
  annRevision: 0,
  annContext: 0,
  annSaveTail: Promise.resolve(),
  annFailedSaves: new Map(),
  annLoadSeq: 0,
  editingId: null,
};
const statuses = [];
const context = {
  state,
  console,
  Promise,
  Map,
  JSON,
  Number,
  Array,
  Error,
  fetch: async () => { throw new Error("unexpected fetch"); },
  activeFrameParams: () => state.plate && state.plate.focus !== null
    ? {t: 0, p: state.plate.focus, z: 0}
    : null,
  annLocked: () => false,
  setAnnStatus: (message) => statuses.push(message),
  basename: (path) => path ? String(path).split("/").pop() : "",
  renderAnnotations: () => {},
  rebuildAnnList: () => {},
  syncFrameScopedControls: () => {},
  $: (id) => id === "ann-editor" ? editor : {},
};
vm.createContext(context);
vm.runInContext(
  section("function normalizeAnnotationSite", "async function flushAnnotationsForUpdate") +
  section("function loadAnnotations", "function setAnnStatus") +
  "\nthis.api = {normalizeAnnotationSite, annotationsUrl, saveAnnotations, loadAnnotations};",
  context
);
const api = context.api;
const emit = (value) => process.stdout.write(JSON.stringify(value));
""" + body + r"""
"""
    result = subprocess.run(
        [NODE, "-e", script, str(APP)],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    return json.loads(result.stdout)


def test_null_empty_and_grid_sites_never_fall_back_to_plate_zero():
    out = _run(
        r"""
(async () => {
  state.annSite = 1;
  const urls = {
    current: api.annotationsUrl(),
    zero: api.annotationsUrl(0),
    nullSite: api.annotationsUrl(null),
    emptySite: api.annotationsUrl(""),
    blankSite: api.annotationsUrl("   "),
    invalidSite: api.annotationsUrl("not-a-site"),
  };
  let fetches = 0;
  context.fetch = async () => {
    fetches += 1;
    return {ok: true, json: async () => ({items: [], path: "wrong.json"})};
  };
  state.plate.focus = null;
  await api.loadAnnotations(null);
  await api.loadAnnotations("");
  await api.loadAnnotations();
  emit({urls, fetches, annSite: state.annSite, ready: state.annReady, statuses});
})().catch((error) => { console.error(error); process.exit(1); });
"""
    )
    assert out["urls"] == {
        "current": "api/annotations?p=1",
        "zero": "api/annotations?p=0",
        "nullSite": None,
        "emptySite": None,
        "blankSite": None,
        "invalidSite": None,
    }
    assert out["fetches"] == 0
    assert out["annSite"] is None
    assert out["ready"] is False
    assert out["statuses"][-1] == "Choose a site to view its annotations"


@pytest.mark.parametrize("save_fails", [False, True])
def test_a_to_b_to_a_keeps_latest_a_snapshot_after_save_success_or_failure(save_fails):
    out = _run(
        rf"""
(async () => {{
  const aUrl = "api/annotations?p=0";
  const bUrl = "api/annotations?p=1";
  const newA = [{{id: "new-a", type: "pin", x: 7, y: 9}}];
  const disk = new Map([
    [aUrl, {{items: [{{id: "old-a"}}], path: "/sidecars/a.json"}}],
    [bUrl, {{items: [{{id: "b"}}], path: "/sidecars/b.json"}}],
  ]);
  const requests = [];
  let failPost = {str(save_fails).lower()};
  context.fetch = async (url, options = {{}}) => {{
    const method = options.method || "GET";
    requests.push(method + " " + url);
    if (method === "POST") {{
      if (failPost) return {{ok: false, status: 503}};
      const body = JSON.parse(options.body);
      disk.set(url, {{items: body.items, path: "/sidecars/a.json"}});
      return {{ok: true, json: async () => ({{path: "/sidecars/a.json"}})}};
    }}
    const data = disk.get(url);
    return {{
      ok: !!data,
      status: data ? 200 : 404,
      json: async () => JSON.parse(JSON.stringify(data)),
    }};
  }};

  // Capture A exactly as setPlateFocus does, then move to and load B while
  // that save is still at the head of the serialization queue.
  state.plate.focus = 0;
  state.annSite = 0;
  state.annotations = newA;
  state.annDirty = true;
  state.annRevision = 1;
  const aSave = api.saveAnnotations(0);
  state.annContext += 1;
  state.annDirty = false;
  state.annSite = null;
  state.plate.focus = 1;
  const bLoad = api.loadAnnotations(1);
  const saveResult = await aSave;
  await bLoad;
  const bItems = state.annotations.map((item) => item.id);

  state.annContext += 1;
  state.annSite = null;
  state.plate.focus = 0;
  await api.loadAnnotations(0);
  const recovered = {{
    items: state.annotations.map((item) => item.id),
    dirty: state.annDirty,
    ready: state.annReady,
    path: state.annPath,
    failedCount: state.annFailedSaves.size,
    status: statuses[statuses.length - 1],
  }};
  let retryResult = null;
  if (failPost) {{
    failPost = false;
    retryResult = await api.saveAnnotations(0);
  }}
  emit({{
    saveResult,
    requests,
    bItems,
    recovered,
    retryResult,
    dirty: state.annDirty,
    ready: state.annReady,
    path: state.annPath,
    failedCount: state.annFailedSaves.size,
    lastStatus: statuses[statuses.length - 1],
  }});
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
    )

    assert out["bItems"] == ["b"]
    assert out["recovered"]["items"] == ["new-a"]
    assert out["recovered"]["ready"] is True
    if save_fails:
        assert out["saveResult"] == {"ok": False, "error": "HTTP 503"}
        assert out["requests"] == [
            "POST api/annotations?p=0",
            "GET api/annotations?p=1",
            "POST api/annotations?p=0",
        ]
        assert out["recovered"]["dirty"] is True
        assert out["recovered"]["path"] is None
        assert out["recovered"]["failedCount"] == 1
        assert out["recovered"]["status"] == "Recovered unsaved annotations · save will retry"
        assert out["retryResult"] == {"ok": True}
        assert out["dirty"] is False
        assert out["failedCount"] == 0
        assert out["lastStatus"] == "Saved · a.json"
    else:
        assert out["saveResult"] == {"ok": True}
        assert out["requests"] == [
            "POST api/annotations?p=0",
            "GET api/annotations?p=1",
            "GET api/annotations?p=0",
        ]
        assert out["recovered"]["dirty"] is False
        assert out["recovered"]["path"] == "/sidecars/a.json"
        assert out["recovered"]["failedCount"] == 0
        assert out["retryResult"] is None
        assert out["dirty"] is False
        assert out["path"] == "/sidecars/a.json"
        assert out["failedCount"] == 0
        assert out["lastStatus"] == "Loaded 1 from a.json"
