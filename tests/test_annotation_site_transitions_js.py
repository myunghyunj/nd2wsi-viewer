"""Annotation routing and site-transition persistence through the store module.

The production store (``annotation-store-v1.js``) is loaded with ``require`` and
driven through its public API with a fake fetch that records call order and can
hold replies or answer 409. ``app.js`` only wraps these calls.
"""

import json
from pathlib import Path

import pytest
from node_host import run_node

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi" / "static"
MODULE = STATIC / "annotation-store-v1.js"
APP = STATIC / "app.js"


def _run(body):
    script = r"""
const { createStore } = require(process.argv[1]);
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
  quitPreparing: false,
};
const statuses = [], notices = [], renders = [];
let timerId = 0;
const timers = new Map();
const context = {
  fetch: async () => { throw new Error("unexpected fetch"); },
  locked: false,
  activeFrameParams: () => state.plate && state.plate.focus !== null
    ? {t: 0, p: state.plate.focus, z: 0}
    : null,
};
const store = createStore({
  state,
  fetch: (...args) => context.fetch(...args),
  isLocked: () => context.locked,
  activeFrame: () => context.activeFrameParams(),
  onStatus: (message) => statuses.push(message),
  onRender: () => renders.push(state.annotations.map((item) => item.id)),
  onEditorReset: () => { editor.hidden = true; },
  onRecoveryNotice: () => notices.push(store.recoveryDrafts() ? "drafts" : "no-drafts"),
  setTimer: (fn, ms) => { const id = ++timerId; timers.set(id, {fn, ms}); return id; },
  clearTimer: (id) => timers.delete(id),
  debounceMs: 800,
});
const runTimers = () => { for (const [id, timer] of [...timers]) { timers.delete(id); timer.fn(); } };
const tick = async (turns = 20) => { for (let i = 0; i < turns; i++) await Promise.resolve(); };
// Historical names kept for tests that still drive saves and loads directly.
const api = {
  normalizeAnnotationSite: store.normalizeSite,
  annotationsUrl: store.urlFor,
  saveAnnotations: store.save,
  loadAnnotations: store.load,
};
// A sidecar "server": GETs read the disk map, POSTs write it, every call is
// recorded in order, and replies can be held or forced to fail.
function sidecarServer(disk, options = {}) {
  const requests = [], held = [], posted = [];
  let revisionCounter = 0;
  const server = {
    requests, held, posted, failPost: options.failPost || null, hold: false, drafts: options.drafts || null,
    release() { for (const finish of held.splice(0)) finish(); },
  };
  context.fetch = (url, init = {}) => {
    const method = init.method || "GET";
    requests.push(method + " " + url);
    const reply = () => {
      if (method === "POST") {
        if (init.headers["Content-Type"] !== "application/json") throw new Error("missing JSON content type");
        const body = JSON.parse(init.body);
        posted.push({url, body});
        if (server.failPost === 409) {
          return {ok: false, status: 409, json: async () => ({
            error: "conflicting annotations", current_revision: "OTHER-WINDOW",
            draft_path: server.drafts ? server.drafts + "/" + posted.length + ".json" : null,
          })};
        }
        if (server.failPost) return {ok: false, status: server.failPost};
        const path = disk.get(url)?.path || "/sidecars/" + url.replace(/\D/g, "") + ".json";
        const revision = "r" + (++revisionCounter);
        disk.set(url, {items: body.items, path, revision});
        return {ok: true, json: async () => ({path, revision})};
      }
      const data = disk.get(url);
      return {ok: !!data, status: data ? 200 : 404, json: async () => JSON.parse(JSON.stringify(data))};
    };
    if (!server.hold) return Promise.resolve().then(reply);
    return new Promise((resolve) => held.push(() => resolve(reply())));
  };
  return server;
}
const snapshot = () => ({
  items: state.annotations.map((item) => item.id),
  site: store.site(), dirty: store.isDirty(), ready: store.isReady(), path: store.path(),
  context: store.context(), failed: store.failedSaves().map((entry) => entry.url),
  pendingTimers: timers.size, status: statuses[statuses.length - 1] || null,
});
const emit = (value) => process.stdout.write(JSON.stringify(value));
""" + body + r"""
"""
    result = run_node(script, MODULE, check=True)
    return json.loads(result.stdout)


def test_app_wrappers_delegate_to_the_store_and_the_module_loads_first():
    app = APP.read_text(encoding="utf-8")
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    assert index.index("annotation-store-v1.js") < index.index("app.js")
    assert "window.Nd2AnnotationStore.createStore({" in app
    for wrapper, method in [
        ("function saveAnnotations(site)", "annotationStore.save(site)"),
        ("function loadAnnotations(site)", "annotationStore.load(site)"),
        ("function queueAnnotationSave(entry)", "annotationStore.queueSave(entry)"),
        ("function annotationRecoveryDrafts()", "annotationStore.recoveryDrafts()"),
        ("async function flushAnnotationsForUpdate()", "annotationStore.flushForUpdate()"),
        ("function annotationsChanged()", "annotationStore.markDirty()"),
    ]:
        start = app.index(wrapper)
        assert method in app[start:app.index("\n}", start)]
    focus = app[app.index("function setPlateFocus(") : app.index("const NATIVE_GESTURE_EXCLUSIONS")]
    assert "annotationStore.saveDeparting()" in focus and "annotationStore.resetOwner()" in focus
    assert "state.annContext" not in focus and "state.annDirty" not in focus
    # only the store writes the persistence fields; the pane edits the items
    block = app[app.index("/* ---- annotations: model") :]
    for field in ("annContext", "annDirty", "annReady", "annSite", "annRevision", "annFailedSaves", "annSaveTail"):
        assert f"state.{field} =" not in block and f"state.{field} +=" not in block


def test_null_empty_and_grid_sites_never_fall_back_to_plate_zero():
    out = _run(
        r"""
(async () => {
  state.annSite = 1;
  const urls = {
    current: store.urlFor(),
    zero: store.urlFor(0),
    nullSite: store.urlFor(null),
    emptySite: store.urlFor(""),
    blankSite: store.urlFor("   "),
    invalidSite: store.urlFor("not-a-site"),
  };
  let fetches = 0;
  context.fetch = async () => {
    fetches += 1;
    return {ok: true, json: async () => ({items: [], path: "wrong.json"})};
  };
  state.plate.focus = null;
  await store.load(null);
  await store.load("");
  await store.load();
  const grid = snapshot();
  const noPlate = {...state, plate: null};
  emit({urls, fetches, grid, statuses, editorHidden: editor.hidden,
        slideUrl: createStore({state: noPlate}).urlFor(5)});
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
    assert out["grid"]["site"] is None and out["grid"]["ready"] is False
    assert out["grid"]["status"] == "Choose a site to view its annotations"
    assert out["editorHidden"] is True
    assert out["slideUrl"] == "api/annotations"


@pytest.mark.parametrize("save_fails", [False, True])
def test_a_to_b_to_a_keeps_latest_a_snapshot_after_save_success_or_failure(save_fails):
    out = _run(
        rf"""
(async () => {{
  const aUrl = "api/annotations?p=0", bUrl = "api/annotations?p=1";
  const server = sidecarServer(new Map([
    [aUrl, {{items: [{{id: "old-a", type: "pin", x: 1, y: 2}}], path: "/sidecars/a.json", revision: "a0"}}],
    [bUrl, {{items: [{{id: "b", type: "pin", x: 1, y: 2}}], path: "/sidecars/b.json", revision: "b0"}}],
  ]));
  state.plate.focus = 0;
  await store.switchOwner(0);
  const loadedA = snapshot();
  // an edit with its debounced save still pending when the site changes
  state.annotations = [{{id: "new-a", type: "pin", x: 7, y: 9}}];
  store.markDirty();
  store.scheduleSave();
  const edited = snapshot();
  server.failPost = {str(save_fails).lower()} ? 503 : null;
  state.plate.focus = 1;
  await store.switchOwner(1);
  const onB = snapshot();
  state.plate.focus = 0;
  await store.switchOwner(0);
  const recovered = snapshot();
  let retry = null;
  if (server.failPost) {{
    server.failPost = null;
    retry = await store.save(0);
  }}
  emit({{loadedA, edited, onB, recovered, retry, final: snapshot(), requests: server.requests,
         posted: server.posted.map((post) => post.body)}});
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
    )
    assert out["loadedA"]["items"] == ["old-a"] and out["loadedA"]["ready"] is True
    assert out["edited"]["dirty"] is True and out["edited"]["pendingTimers"] == 1
    # the switch called the debounce off and captured A before B was read
    assert out["onB"]["items"] == ["b"] and out["onB"]["dirty"] is False
    assert out["onB"]["pendingTimers"] == 0 and out["onB"]["site"] == 1
    assert out["recovered"]["items"] == ["new-a"] and out["recovered"]["ready"] is True
    assert out["posted"][0] == {"items": [{"id": "new-a", "type": "pin", "x": 7, "y": 9}], "expected_revision": "a0"}
    if save_fails:
        assert out["requests"] == [
            "GET api/annotations?p=0",
            "POST api/annotations?p=0",
            "GET api/annotations?p=1",
            "POST api/annotations?p=0",
        ]
        assert out["recovered"]["dirty"] is True and out["recovered"]["path"] is None
        assert out["recovered"]["failed"] == ["api/annotations?p=0"]
        assert out["recovered"]["status"] == "Recovered unsaved annotations · save will retry"
        assert out["retry"] == {"ok": True}
        assert out["final"]["dirty"] is False and out["final"]["failed"] == []
        assert out["final"]["status"] == "Saved · a.json"
    else:
        assert out["requests"] == [
            "GET api/annotations?p=0",
            "POST api/annotations?p=0",
            "GET api/annotations?p=1",
            "GET api/annotations?p=0",
        ]
        assert out["recovered"]["dirty"] is False and out["recovered"]["path"] == "/sidecars/a.json"
        assert out["recovered"]["failed"] == [] and out["retry"] is None
        assert out["recovered"]["status"] == "Loaded 1 from a.json"


def test_save_in_flight_during_switch_finishes_first_and_cannot_dirty_the_new_owner():
    out = _run(
        r"""
(async () => {
  const aUrl = "api/annotations?p=0", bUrl = "api/annotations?p=1";
  const server = sidecarServer(new Map([
    [aUrl, {items: [], path: "/sidecars/a.json", revision: "a0"}],
    [bUrl, {items: [{id: "b", type: "pin", x: 1, y: 2}], path: "/sidecars/b.json", revision: "b0"}],
  ]));
  state.plate.focus = 0;
  await store.switchOwner(0);
  state.annotations = [{id: "late-a", type: "pin", x: 1, y: 2}];
  store.markDirty();
  server.hold = true;
  server.failPost = 503;
  state.plate.focus = 1;
  const switching = store.switchOwner(1);
  await tick();
  const whileSaving = {...snapshot(), requests: [...server.requests], statuses: [...statuses]};
  server.release(); // A's save fails only now, after B is the owner
  await tick();
  const afterFailure = {...snapshot(), requests: [...server.requests], statuses: [...statuses]};
  server.release(); // B's GET
  await switching;
  const onB = snapshot();
  state.plate.focus = 0;
  server.hold = false;
  await store.switchOwner(0);
  emit({whileSaving, afterFailure, onB, backOnA: snapshot(), requests: server.requests});
})().catch((error) => { console.error(error); process.exit(1); });
"""
    )
    assert out["whileSaving"]["requests"] == ["GET api/annotations?p=0", "POST api/annotations?p=0"]
    assert out["whileSaving"]["site"] == 1 and out["whileSaving"]["ready"] is False
    assert out["whileSaving"]["dirty"] is False and out["whileSaving"]["items"] == []
    # the departing site's failure is recorded for retry but never shown as
    # the new owner's status or dirty flag
    assert out["afterFailure"]["failed"] == ["api/annotations?p=0"]
    assert out["afterFailure"]["dirty"] is False
    assert out["afterFailure"]["statuses"] == out["whileSaving"]["statuses"]
    assert out["afterFailure"]["requests"][-1] == "GET api/annotations?p=1"
    assert out["onB"]["items"] == ["b"] and out["onB"]["ready"] is True and out["onB"]["dirty"] is False
    assert out["backOnA"]["items"] == ["late-a"] and out["backOnA"]["dirty"] is True
    assert out["backOnA"]["status"] == "Recovered unsaved annotations · save will retry"
    assert out["requests"].count("GET api/annotations?p=0") == 1


def test_conflict_keeps_the_draft_and_only_explicit_acceptance_lets_the_flush_resolve():
    out = _run(
        r"""
(async () => {
  const aUrl = "api/annotations?p=0";
  const server = sidecarServer(new Map([[aUrl, {items: [{id: "disk", type: "pin", x: 1, y: 2}], path: "/sidecars/a.json", revision: "r0"}]]),
    {drafts: "/session/drafts"});
  state.plate.focus = 0;
  await store.switchOwner(0);
  state.annotations = [{id: "mine"}];
  store.markDirty();
  server.failPost = 409;
  const conflict = await store.save();
  const afterConflict = {...snapshot(), drafts: store.recoveryDrafts()?.map((entry) => entry.draftPath),
    accepted: store.failedSaves().map(store.draftAccepted), serverRevision: store.serverRevision()};
  const flushes = [];
  const flush = async () => {
    state.quitPreparing = true;
    try { await store.flushForUpdate(); flushes.push("resolved"); }
    catch (error) { flushes.push(error.message); }
    state.quitPreparing = false;
  };
  await flush();
  state.quitPreparing = true;
  const acceptedWhileClosing = store.acceptRecoveryDrafts();
  state.quitPreparing = false;
  const accepted = store.acceptRecoveryDrafts();
  const afterAccept = {...snapshot(), accepted: store.failedSaves().map(store.draftAccepted)};
  await flush();
  // a newer edit revokes the choice: the flush must try the new body again
  state.annotations = [{id: "mine"}, {id: "newer"}];
  store.markDirty();
  const revoked = store.failedSaves().map(store.draftAccepted);
  await flush();
  emit({conflict, afterConflict, acceptedWhileClosing, accepted, afterAccept, flushes, revoked,
        final: snapshot(), posted: server.posted.map((post) => post.body), requests: server.requests, notices});
})().catch((error) => { console.error(error); process.exit(1); });
"""
    )
    assert out["conflict"]["ok"] is False
    assert out["conflict"]["error"].startswith("Conflict: another window changed this sidecar.")
    assert "Recovery draft: /session/drafts/1.json" in out["conflict"]["error"]
    assert out["afterConflict"]["items"] == ["mine"] and out["afterConflict"]["dirty"] is True
    assert out["afterConflict"]["failed"] == ["api/annotations?p=0"]
    assert out["afterConflict"]["drafts"] == ["/session/drafts/1.json"]
    assert out["afterConflict"]["accepted"] == [False]
    assert out["afterConflict"]["serverRevision"] == "r0"  # never the other window's
    assert out["acceptedWhileClosing"] is False and out["accepted"] is True
    assert out["afterAccept"]["accepted"] == [True]
    assert out["flushes"][0].startswith("Annotations conflict.")
    assert out["flushes"][1] == "resolved"
    assert out["revoked"] == [False]
    assert out["flushes"][2].startswith("Annotations conflict.")
    # kept-for-closing never means the sidecar was written: the draft stays
    assert out["final"]["dirty"] is True and out["final"]["failed"] == ["api/annotations?p=0"]
    assert [post["expected_revision"] for post in out["posted"]] == ["r0", "r0", "r0"]
    assert out["posted"][-1]["items"] == [{"id": "mine"}, {"id": "newer"}]
    assert out["requests"].count("GET api/annotations?p=0") == 1
    assert "drafts" in out["notices"]


def test_flush_for_update_resolves_only_after_queued_saves_complete_in_order():
    out = _run(
        r"""
(async () => {
  const aUrl = "api/annotations?p=0";
  const server = sidecarServer(new Map([[aUrl, {items: [], path: "/sidecars/a.json", revision: "r0"}]]));
  state.plate.focus = 0;
  await store.switchOwner(0);
  server.hold = true;
  state.annotations = [{id: "first"}];
  store.markDirty();
  const first = store.save();
  state.annotations = [{id: "first"}, {id: "second"}];
  store.markDirty();
  store.scheduleSave();
  let settled = null;
  state.quitPreparing = true;
  const flush = store.flushForUpdate().then(() => { settled = "resolved"; }, (error) => { settled = error.message; });
  await tick();
  const whileFirstHeld = {settled, requests: [...server.requests], pendingTimers: timers.size};
  server.release(); // first save completes; the newer snapshot must follow
  await tick();
  const whileSecondHeld = {settled, requests: [...server.requests], dirty: store.isDirty()};
  server.release();
  await flush;
  emit({firstResult: await first, whileFirstHeld, whileSecondHeld, settled, final: snapshot(),
        posted: server.posted.map((post) => post.body), tailResolved: await store.tail().then(() => true)});
})().catch((error) => { console.error(error); process.exit(1); });
"""
    )
    assert out["firstResult"] == {"ok": True}
    assert out["whileFirstHeld"] == {
        "settled": None, "requests": ["GET api/annotations?p=0", "POST api/annotations?p=0"], "pendingTimers": 0,
    }
    assert out["whileSecondHeld"]["settled"] is None and out["whileSecondHeld"]["dirty"] is True
    assert out["whileSecondHeld"]["requests"] == [
        "GET api/annotations?p=0", "POST api/annotations?p=0", "POST api/annotations?p=0",
    ]
    assert out["settled"] == "resolved"
    assert out["final"]["dirty"] is False and out["final"]["failed"] == []
    assert out["final"]["status"] == "Saved · a.json"
    assert out["posted"] == [
        {"items": [{"id": "first"}], "expected_revision": "r0"},
        {"items": [{"id": "first"}, {"id": "second"}], "expected_revision": "r1"},
    ]
    assert out["tailResolved"] is True


def test_dirty_marks_without_a_known_owner_block_the_switch():
    out = _run(
        r"""
(async () => {
  const server = sidecarServer(new Map());
  state.plate.focus = null;
  state.annotations = [{id: "orphan"}];
  store.markDirty();
  store.scheduleSave();
  const before = snapshot();
  const switched = await store.switchOwner(1);
  emit({before, switched, after: snapshot(), requests: server.requests});
})().catch((error) => { console.error(error); process.exit(1); });
"""
    )
    assert out["before"]["pendingTimers"] == 1 and out["before"]["dirty"] is True
    assert out["switched"] is False
    assert out["after"]["status"] == "Not saved: annotation owner is unknown"
    assert out["after"]["items"] == ["orphan"] and out["after"]["dirty"] is True
    assert out["after"]["context"] == out["before"]["context"]
    assert out["after"]["pendingTimers"] == 0
    assert out["requests"] == []


def test_older_load_cannot_install_over_a_newer_one():
    out = _run(
        r"""
(async () => {
  const aUrl = "api/annotations?p=0", bUrl = "api/annotations?p=1";
  const server = sidecarServer(new Map([
    [aUrl, {items: [{id: "a", type: "pin", x: 1, y: 2}], path: "/sidecars/a.json", revision: "a0"}],
    [bUrl, {items: [{id: "b", type: "pin", x: 1, y: 2}], path: "/sidecars/b.json", revision: "b0"}],
  ]));
  server.hold = true;
  state.plate.focus = 0;
  const first = store.switchOwner(0);
  await tick();
  state.plate.focus = 1;
  const second = store.switchOwner(1);
  await tick();
  const inFlight = {...snapshot(), requests: [...server.requests], held: server.held.length};
  server.held.shift()(); // A answers first, after B became the owner
  await tick();
  const afterStaleReply = snapshot();
  server.release();
  await Promise.all([first, second]);
  emit({inFlight, afterStaleReply, final: snapshot(), renders});
})().catch((error) => { console.error(error); process.exit(1); });
"""
    )
    assert out["inFlight"]["requests"] == ["GET api/annotations?p=0", "GET api/annotations?p=1"]
    assert out["inFlight"]["held"] == 2 and out["inFlight"]["site"] == 1
    assert out["afterStaleReply"]["items"] == [] and out["afterStaleReply"]["ready"] is False
    assert out["final"]["items"] == ["b"] and out["final"]["ready"] is True
    assert out["final"]["path"] == "/sidecars/b.json" and out["final"]["status"] == "Loaded 1 from b.json"
    assert ["a"] not in out["renders"]


@pytest.mark.parametrize("raw", [True, [], "broken", {"unknown": 1},
    {"isolation_highwater": -1, "presses": {"bad/path": 4}},
    {"isolation_highwater": 9, "presses": {"good": 3, "bad/path": 4}, "extra": 1}])
def test_laser_metadata_is_sanitized_on_load_before_the_next_save(raw):
    from nd2wsi.slacs_laser import metadata

    out = _run("const raw=" + json.dumps(raw) + r''';
(async()=>{
 const item={id:'laser',type:'box',x:1,y:2,w:3,h:4,slacs:{kind:'laser_shot',isolation:7}};
 const server=sidecarServer(new Map([['api/annotations?p=0',{revision:'r0',items:[item],slacs:raw}]]));
 state.plate.focus=0;await store.load(0);const loaded=state.annLaserState;
 store.markDirty();await store.save();
 emit({loaded,saved:server.posted[0].body.slacs,dirty:state.annDirty,items:state.annotations});
})().catch(e=>{console.error(e);process.exit(1);});
''')
    assert metadata(out["saved"])["slacs"] == out["loaded"]
    assert out["saved"]["isolation_highwater"] == (9 if isinstance(raw, dict) and raw.get("extra") else 7)
    assert "bad/path" not in out["saved"]["presses"] and not out["dirty"]
    assert out["items"][0]["slacs"]["isolation"] == 7
