"""Annotation store auto-save additions: save-state events, periodic retry,
the pagehide flush and the keepalive body cap.

The production store (``annotation-store-v1.js``) is loaded with ``require``
and driven with a fake fetch that records every request and its init.
"""

import json
from pathlib import Path

from node_host import run_node

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi" / "static"
MODULE = STATIC / "annotation-store-v1.js"

HARNESS = r"""
const { createStore } = require(process.argv[1]);
let now = 500000, timerId = 0;
const timers = new Map(), events = [], statuses = [], requests = [];
const state = {
  plate: {focus: 0},
  info: {plate: {P: 3}, notes: []},
  annotations: [], annPath: null, annSite: 0, annReady: true, annDirty: false,
  annRevision: 0, annContext: 0, annSaveTail: Promise.resolve(), annFailedSaves: new Map(),
  annLoadSeq: 0, editingId: null, quitPreparing: false,
};
const server = {fail: new Map(), gate: null, revision: 0}; // url -> HTTP status to answer with; gate: a promise every reply waits for
const context = {locked: false};
const store = createStore({
  state,
  fetch: (url, init = {}) => {
    requests.push({method: init.method || "GET", url, keepalive: init.keepalive === true ? true : undefined,
      body: init.body ? JSON.parse(init.body).items.map((item) => item.id) : null,
      slacs: init.body ? JSON.parse(init.body).slacs : undefined,
      bytes: init.body ? Buffer.byteLength(init.body) : 0});
    return (server.gate || Promise.resolve()).then(() => {
      const status = server.fail.get(url);
      if (status === 409) return {ok: false, status, json: async () => ({draft_path: "/drafts/" + requests.length + ".json"})};
      if (status) return {ok: false, status};
      return {ok: true, json: async () => ({path: "/sidecars/" + url.replace(/\D/g, "") + ".json", revision: "r" + (++server.revision)})};
    });
  },
  isLocked: () => context.locked,
  activeFrame: () => state.plate.focus === null ? null : {t: 0, p: state.plate.focus, z: 0},
  onStatus: (message) => statuses.push(message),
  onSaveState: (event) => events.push(event),
  now: () => now,
  setTimer: (fn, ms) => { const id = ++timerId; timers.set(id, {fn, ms}); return id; },
  clearTimer: (id) => timers.delete(id),
});
const runTimers = () => { for (const [id, timer] of [...timers]) { timers.delete(id); timer.fn(); } };
const tick = async (turns = 30) => { for (let i = 0; i < turns; i++) await Promise.resolve(); };
const edit = (id) => { state.annotations = [...state.annotations, {id, type: "pin", x: 1, y: 2}]; store.markDirty(); store.scheduleSave(); };
const posts = () => requests.filter((r) => r.method === "POST").map((r) => r.url + (r.keepalive ? " keepalive" : ""));
// a plate focus change as the pane performs it: the departing site's dirty
// marks are captured (and re-sent even after a failure), then the next owner
const switchTo = async (site) => {
  store.saveDeparting(); store.resetOwner();
  state.plate.focus = site; state.annSite = site; state.annReady = true; state.annotations = [];
  await tick();
};
const states = (from = 0) => events.slice(from).map((e) => e.state);
const emit = (value) => process.stdout.write(JSON.stringify(value));
"""


def _run(body):
    result = run_node(
        HARNESS + "(async () => {" + body + "})().catch((error) => { console.error(error); process.exit(1); });",
        MODULE, check=True,
    )
    return json.loads(result.stdout)


def test_an_edit_reports_dirty_then_saving_then_saved_with_the_injected_clock():
    out = _run("""
edit("a");
const afterEdit = events.map((e) => [e.channel, e.state, e.at]);
now += 800; runTimers(); await tick();
emit({afterEdit, events, posts: posts(), dirty: state.annDirty, statuses});
""")
    assert out["afterEdit"] == [["annotations", "dirty", 500000]]
    assert [e["state"] for e in out["events"]] == ["dirty", "saving", "saved"]
    assert all(e["channel"] == "annotations" for e in out["events"])
    assert [e["at"] for e in out["events"]][1:] == [500800, 500800]
    assert out["posts"] == ["api/annotations?p=0"] and out["dirty"] is False
    assert out["statuses"] == ["Saved · 0.json"]


def test_transient_failures_report_failed_and_409_reports_conflict_without_changing_status_text():
    out = _run("""
server.fail.set("api/annotations?p=0", 500);
edit("a"); runTimers(); await tick();
const failed = events[events.length - 1];
server.fail.set("api/annotations?p=0", 409);
edit("b"); runTimers(); await tick();
const conflict = events[events.length - 1];
emit({states: states(), failed, conflict, statuses, entries: store.failedSaves().map((e) => [e.url, e.conflict, e.error])});
""")
    assert out["states"] == ["dirty", "saving", "failed", "dirty", "saving", "conflict"]
    assert out["failed"]["conflict"] is False and out["failed"]["message"] == "HTTP 500"
    assert out["conflict"]["conflict"] is True and out["conflict"]["message"].startswith("Conflict: another window changed this sidecar.")
    assert out["statuses"] == ["Save failed: HTTP 500", "Save failed: " + out["conflict"]["message"]]
    assert out["entries"] == [["api/annotations?p=0", True, out["conflict"]["message"]]]


def test_a_success_for_one_site_still_reports_an_older_sites_conflict_or_failure():
    out = _run("""
server.fail.set("api/annotations?p=0", 409);
edit("a"); runTimers(); await tick();
// leave the conflicted site: the departing capture 409s again and the
// snapshot stays in annFailedSaves
await switchTo(1);
const departed = states();
const before = events.length;
edit("b"); runTimers(); await tick();
const afterConflict = states(before);
// a transient failure on another site while the conflict stands
state.annFailedSaves.get("api/annotations?p=0").conflict = false;
server.fail.set("api/annotations?p=1", 500);
const mid = events.length;
edit("c"); runTimers(); await tick();
emit({departed, afterConflict, afterFailed: states(mid), last: events[events.length - 1], posts: posts()});
""")
    assert out["departed"] == ["dirty", "saving", "conflict", "saving", "conflict"]
    assert out["afterConflict"] == ["dirty", "saving", "saved", "conflict"]
    assert out["afterFailed"] == ["dirty", "saving", "failed"]
    assert out["last"]["conflict"] is False and out["last"]["message"] == "HTTP 500"
    assert out["posts"] == ["api/annotations?p=0"] * 2 + ["api/annotations?p=1"] * 2


def test_a_save_that_lands_while_newer_edits_are_pending_reports_saved_then_dirty():
    out = _run("""
edit("a"); runTimers();
await Promise.resolve(); // the queued request has started
edit("b"); // newer revision while the first request is in flight
await tick();
runTimers(); await tick();
emit({states: states(), posts: posts(), dirty: state.annDirty});
""")
    assert out["states"] == ["dirty", "saving", "dirty", "saved", "dirty", "saving", "saved"]
    assert out["posts"] == ["api/annotations?p=0", "api/annotations?p=0"] and out["dirty"] is False


def test_retry_failed_requeues_transient_failures_only_and_never_touches_a_conflict():
    out = _run("""
server.fail.set("api/annotations?p=0", 500);
edit("a"); runTimers(); await tick();
await switchTo(1); // the departing capture fails again: two p=0 requests
server.fail.set("api/annotations?p=1", 409);
edit("b"); runTimers(); await tick();
await switchTo(2); // likewise two p=1 requests, both 409
const before = {posts: posts(), failed: store.failedSaves().map((e) => [e.url, e.conflict]),
  events: events.length, statuses: statuses.length};
server.fail.clear();
const result = await store.retryFailed();
await tick();
const again = await store.retryFailed();
emit({before, result, again, posts: posts(), failed: store.failedSaves().map((e) => [e.url, e.conflict]),
  states: states(before.events), statuses: statuses.slice(before.statuses)});
""")
    assert out["before"]["posts"] == ["api/annotations?p=0"] * 2 + ["api/annotations?p=1"] * 2
    assert out["before"]["failed"] == [["api/annotations?p=0", False], ["api/annotations?p=1", True]]
    assert out["result"] == {"ok": True, "retried": 1}
    # only the transient site was re-queued; the 409 snapshot was never sent again
    assert out["posts"] == ["api/annotations?p=0"] * 2 + ["api/annotations?p=1"] * 2 + ["api/annotations?p=0"]
    assert out["failed"] == [["api/annotations?p=1", True]]
    assert out["again"] == {"ok": True, "retried": 0}
    # the retry's success still reports the standing conflict, and writes no
    # panel status for a site that is no longer the owner
    assert out["states"] == ["saving", "saved", "conflict"]
    assert out["statuses"] == []


def test_retry_failed_saves_a_dirty_snapshot_only_when_no_debounce_is_pending_and_never_a_conflicted_site():
    out = _run("""
edit("a");
const pending = await store.retryFailed(); // the 800 ms debounce owns this save
const afterPending = posts();
runTimers(); await tick();
state.annotations = [...state.annotations, {id: "b", type: "pin", x: 3, y: 4}];
store.markDirty(); // dirty without a scheduled save, e.g. after a cancelled update preparation
const idle = await store.retryFailed();
const afterIdle = posts();
// a conflicted current site: the dirty snapshot is left to the recovery notice
server.fail.set("api/annotations?p=0", 409);
edit("c"); runTimers(); await tick();
const conflicted = await store.retryFailed();
emit({pending, afterPending, idle, afterIdle, conflicted, posts: posts(), dirty: store.isDirty()});
""")
    assert out["pending"] == {"ok": True, "retried": 0} and out["afterPending"] == []
    assert out["idle"] == {"ok": True, "retried": 1}
    assert out["afterIdle"] == ["api/annotations?p=0", "api/annotations?p=0"]
    assert out["conflicted"] == {"ok": True, "retried": 0}
    assert out["posts"] == ["api/annotations?p=0"] * 3
    assert out["dirty"] is True


def test_retry_failed_waits_for_an_in_flight_save_and_never_queues_a_revision_twice():
    out = _run("""
let release; server.gate = new Promise((resolve) => { release = resolve; });
edit("a"); runTimers(); await tick(); // revision 1 has left and waits on the slow server
const retrying = store.retryFailed(); // the minute tick during the slow request
await tick();
const duringRetry = posts();
// a second edit queues revision 2 behind the slow request while the retry waits
edit("b"); runTimers();
server.gate = null; release();
const retried = await retrying;
await tick();
emit({duringRetry, retried, bodies: requests.filter((r) => r.method === "POST").map((r) => r.body), dirty: state.annDirty});
""")
    assert out["duringRetry"] == ["api/annotations?p=0"]
    assert out["retried"] == {"ok": True, "retried": 0}
    # one request per revision: neither the slow save nor the one queued behind it was duplicated
    assert out["bodies"] == [["a"], ["a", "b"]]
    assert out["dirty"] is False


def test_retry_failed_still_saves_a_recovered_snapshot_whose_failed_requests_belonged_to_an_older_owner():
    out = _run("""
server.fail.set("api/annotations?p=0", 500);
edit("a"); runTimers(); await tick();
await switchTo(1); // the departing capture fails too; both requests carried the old context
store.resetOwner(); state.plate.focus = 0; await store.load(0); await tick();
const recovered = {dirty: state.annDirty, items: state.annotations.map((a) => a.id), status: statuses[statuses.length - 1]};
server.fail.clear();
const retried = await store.retryFailed();
emit({recovered, retried, posts: posts(), dirty: state.annDirty, failed: store.failedSaves().length, last: states().slice(-1)});
""")
    assert out["recovered"] == {"dirty": True, "items": ["a"], "status": "Recovered unsaved annotations · save will retry"}
    assert out["retried"] == {"ok": True, "retried": 1}
    assert out["posts"] == ["api/annotations?p=0"] * 3
    # the retry sent the current owner's snapshot, so its success cleared the dirty flag
    assert out["dirty"] is False and out["failed"] == 0 and out["last"] == ["saved"]


def test_an_edit_made_while_a_sidecar_failed_to_load_is_never_sent_but_other_sites_still_retry():
    # The load emptied the list first: sending it would replace site 1's
    # sidecar with only the mark made meanwhile.
    out = _run("""
server.fail.set("api/annotations?p=0", 500).set("api/annotations?p=1", 500);
edit("a"); runTimers(); await tick(); // site 0's save fails and waits for a retry
store.resetOwner(); state.plate.focus = 1;
const loading = store.load(1);
edit("b"); // before the load answered
await loading; await tick();
const failed = {loadFailed: state.annLoadFailed, dirty: state.annDirty, timers: timers.size};
server.fail.clear();
const pagehide = await store.flushOnPagehide();
const retried = await store.retryFailed();
let flushed = "ok";
try { await store.flushForUpdate(); } catch (error) { flushed = error.message; }
emit({failed, pagehide, retried, posts: posts(), flushed});
""")
    assert out["failed"] == {"loadFailed": True, "dirty": False, "timers": 0}
    assert out["pagehide"] == {"ok": True, "sent": False}
    assert out["retried"] == {"ok": True, "retried": 1}
    assert out["posts"] == ["api/annotations?p=0"] * 2
    assert out["flushed"] == "ok"


def test_retry_and_pagehide_respect_a_locked_pane_and_a_missing_plate_site_without_status_text():
    out = _run("""
server.fail.set("api/annotations?p=0", 500);
edit("a"); runTimers(); await tick();
server.fail.clear();
const statusCount = statuses.length, eventCount = events.length;
context.locked = true;
const locked = await store.retryFailed();
const lockedFlush = await store.flushOnPagehide();
context.locked = false;
state.plate.focus = null; state.annSite = null;
state.annFailedSaves.clear(); // dirty marks with no owner
const noSite = await store.retryFailed();
const noSiteFlush = await store.flushOnPagehide();
emit({locked, lockedFlush, noSite, noSiteFlush, posts: posts(), statuses: statuses.slice(statusCount), events: states(eventCount)});
""")
    assert out["locked"] == {"ok": False, "error": "annotation is locked", "retried": 0}
    assert out["lockedFlush"] == {"ok": False, "error": "annotation is locked", "sent": False}
    assert out["noSite"] == {"ok": True, "retried": 0}
    assert out["noSiteFlush"] == {"ok": True, "sent": False}
    assert out["posts"] == ["api/annotations?p=0"]
    assert out["statuses"] == [] and out["events"] == []


def test_save_and_retry_forward_keepalive_and_the_request_leaves_once_microtasks_drain():
    out = _run("""
edit("a");
store.cancelScheduledSave();
const saving = store.save(undefined, {keepalive: true});
const synchronous = requests.length;
await Promise.resolve(); await Promise.resolve();
const drained = requests.map((r) => [r.url, r.keepalive]);
await saving;
edit("b"); runTimers(); await tick();
server.fail.set("api/annotations?p=0", 503);
edit("c"); runTimers(); await tick();
server.fail.clear();
await store.retryFailed({keepalive: true});
emit({synchronous, drained, requests: requests.map((r) => [r.url, r.keepalive === undefined ? "plain" : "keepalive"])});
""")
    assert out["synchronous"] == 0
    assert out["drained"] == [["api/annotations?p=0", True]]
    assert out["requests"] == [
        ["api/annotations?p=0", "keepalive"], ["api/annotations?p=0", "plain"],
        ["api/annotations?p=0", "plain"], ["api/annotations?p=0", "keepalive"],
    ]


def test_pagehide_flush_skips_a_snapshot_its_recovery_draft_holds_and_sends_anything_newer_or_transient():
    out = _run("""
server.fail.set("api/annotations?p=0", 409);
edit("a"); runTimers(); await tick();
// the 409 wrote a draft of this exact body: a close or reload sends nothing
const held = await store.flushOnPagehide({keepalive: true});
const afterHeld = {posts: posts(), dirty: state.annDirty};
// coming back to the site loads the recovered conflict: dirty again, still nothing new to send
await switchTo(1); // the departing capture 409s once more
store.resetOwner(); state.plate.focus = 0; await store.load(0); await tick();
const recovered = {dirty: state.annDirty, items: state.annotations.map((a) => a.id),
  flush: await store.flushOnPagehide({keepalive: true}), posts: posts().length};
// a newer edit on the conflicted site is a snapshot the draft lacks: the
// pending debounce is called off and it goes out (and 409s into a new draft)
edit("b");
const newer = await store.flushOnPagehide({keepalive: true});
const afterNewer = {posts: posts(), timers: timers.size, drafts: store.failedSaves().map((e) => [e.conflict, e.draftPath !== null])};
const heldAgain = await store.flushOnPagehide({keepalive: true});
// a transient failure is re-sent
server.fail.set("api/annotations?p=0", 500);
edit("c"); runTimers(); await tick();
server.fail.clear();
const transient = await store.flushOnPagehide({keepalive: true});
const clean = await store.flushOnPagehide({keepalive: true});
emit({held, afterHeld, recovered, newer, afterNewer, heldAgain, transient, clean, posts: posts(),
  dirty: state.annDirty, failed: store.failedSaves().length});
""")
    p0 = "api/annotations?p=0"
    assert out["held"] == {"ok": True, "sent": False}
    assert out["afterHeld"] == {"posts": [p0], "dirty": True}
    assert out["recovered"] == {"dirty": True, "items": ["a"], "flush": {"ok": True, "sent": False}, "posts": 2}
    assert out["newer"]["sent"] is True and out["newer"]["ok"] is False
    assert out["newer"]["error"].startswith("Conflict: another window changed this sidecar.")
    assert out["afterNewer"] == {"posts": [p0, p0, p0 + " keepalive"], "timers": 0, "drafts": [[True, True]]}
    assert out["heldAgain"] == {"ok": True, "sent": False}
    assert out["transient"] == {"ok": True, "sent": True}
    assert out["clean"] == {"ok": True, "sent": False}
    assert out["posts"] == [p0, p0, p0 + " keepalive", p0, p0 + " keepalive"]
    assert out["dirty"] is False and out["failed"] == 0


def test_pagehide_keepalive_is_dropped_for_a_snapshot_over_the_fetch_body_limit():
    out = _run("""
const path = require("path");
const {KEEPALIVE_BODY_LIMIT} = require(process.argv[1]);
const lut = require(path.join(path.dirname(process.argv[1]), "lut-persistence-v1.js"));
// 33,000 two-byte characters: 66,000 UTF-8 bytes from a string of 33,000 characters,
// so a count of characters instead of bytes would have kept keepalive on
state.annotations = [{id: "big", type: "pin", x: 1, y: 2, text: "é".repeat(33000)}];
store.markDirty(); store.scheduleSave();
const big = await store.flushOnPagehide({keepalive: true});
state.annotations = [{id: "small", type: "pin", x: 1, y: 2}];
store.markDirty(); store.scheduleSave();
const small = await store.flushOnPagehide({keepalive: true});
emit({limit: KEEPALIVE_BODY_LIMIT, lutLimit: lut.KEEPALIVE_BODY_LIMIT, big, small, timers: timers.size,
  requests: requests.map((r) => [r.body, r.keepalive === true ? "keepalive" : "plain", r.bytes])});
""")
    assert out["limit"] == 60 * 1024 and out["lutLimit"] == 60 * 1024
    assert out["big"] == {"ok": True, "sent": True} and out["small"] == {"ok": True, "sent": True}
    assert out["timers"] == 0
    (big_body, big_mode, big_bytes), (small_body, small_mode, small_bytes) = out["requests"]
    assert big_body == ["big"] and big_mode == "plain" and big_bytes > 64 * 1024
    assert small_body == ["small"] and small_mode == "keepalive" and small_bytes < 200


def test_isolation_metadata_survives_conflict_recovery_and_stays_with_departing_site():
    out = _run("""
const meta={isolation_highwater:8,presses:{'receiver_with-hyphen':55}};
state.annLaserState=meta;
server.fail.set('api/annotations?p=0',409);
edit('laser');runTimers();await tick();
await switchTo(1);
state.annLaserState={isolation_highwater:2,presses:{}};
edit('other');runTimers();await tick();
store.saveDeparting();store.resetOwner();state.plate.focus=0;await store.load(0);await tick();
emit({posts:requests.filter(r=>r.method==='POST').map(r=>[r.url,r.slacs]),
 recovered:state.annLaserState,items:state.annotations.map(a=>a.id),conflict:store.failedSaves().some(e=>e.conflict)});
""")
    assert out["recovered"] == {"isolation_highwater": 8, "presses": {"receiver_with-hyphen": 55}}
    assert out["items"] == ["laser"] and out["conflict"]
    assert all(meta["isolation_highwater"] == (8 if url.endswith("0") else 2) for url, meta in out["posts"])
