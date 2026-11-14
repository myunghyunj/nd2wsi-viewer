"""Regression tests for SUA-1 through SUA-7 and SW-4 (2.6.3 audit repairs)."""

from __future__ import annotations

import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import zarr
from http_fixture import serving
from node_host import run_node
from PIL import Image

from nd2wsi import annotation_workspace
from nd2wsi.annotation_workspace import AnnotationWorkspace
from nd2wsi.cache import cache_container, quick_fingerprint
from nd2wsi.lut_settings import FORMAT as LUT_FORMAT
from nd2wsi.lut_settings import LutSettingsStore
from nd2wsi.server import (
    SlideRegistry,
    ViewerState,
    annotations_sidecar,
    confocal_annotations_sidecar,
    create_server,
    plate_annotations_sidecar,
)
from nd2wsi.session_lock import SessionFileLock
from nd2wsi.storage.single_file import SQLiteStore, open_zarr_group
from nd2wsi.window_sessions import create_window_session

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "nd2wsi" / "static"


def _run_js(script: str) -> object:
    wrapped = (
        "(async () => {\n"
        + script
        + "\n})().then((v) => process.stdout.write(JSON.stringify(v)))"
        ".catch((e) => { console.error(e); process.exit(1); });"
    )
    result = run_node(wrapped, str(STATIC), check=True)
    return json.loads(result.stdout)


def _http_request(
    url: str,
    *,
    payload: bytes | dict | None = None,
    method: str | None = None,
) -> tuple[int, dict]:
    if isinstance(payload, dict):
        body = json.dumps(payload).encode("utf-8")
    else:
        body = payload
    req = urllib.request.Request(
        url,
        data=body,
        method=method or ("POST" if body is not None else "GET"),
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


# ---- SUA-1: tool disarm on plane switch + readiness/lock guards --------------


def test_sua1_plate_frame_transition_disarms_annotation_tools_and_keeps_roi():
    result = _run_js(r"""
const fs = require("fs"), vm = require("vm"), path = require("path");
const source = fs.readFileSync(path.join(process.argv[1], "app.js"), "utf8").replace(/\r\n/g, "\n");
const fnSource = (name) => {
  const start = source.indexOf("function " + name + "("), end = source.indexOf("\n}\n", start);
  if (start < 0 || end < 0) throw new Error("missing: " + name);
  return source.slice(start, end) + "\n}\n";
};
const nodes = new Map();
const $ = (id) => {
  if (!nodes.has(id)) {
    nodes.set(id, {
      id, hidden: true, style: {}, textContent: "", disabled: false, dataset: {},
      classList: { add() {}, remove() {}, toggle() {} },
    });
  }
  return nodes.get(id);
};
let dragCancelled = 0, switched = 0;
const state = {
  tool: null, plate: { focus: 0 }, roi: null, inspect: null, quitPreparing: false,
  annReady: true, info: { width: 100, height: 100, storage: "full", plate: { annotationScope: "plane" } },
  viewer: { setMouseNavEnabled() {} },
  windows: { region: { isHidden: () => true, open() {} }, info: { isHidden: () => true } },
  cancelToolDrag: () => { dragCancelled += 1; },
};
const context = vm.createContext({
  state, $, Object, Boolean,
  TOOL_BTN: { roi: "roi-toggle", move: "roi-move", measure: "tool-measure", pin: "tool-pin", box: "tool-box", astromap: "astromap-draw" },
  activeFrameParams: () => ({ t: 0, p: 0, z: 1 }),
  annotationOwnerReady: () => state.annReady,
  annLocked: () => false,
  frameOwnsRoi: () => false,
  setAnnStatus() {}, showToast() {}, renderAnnotations() {}, closeEditor() {},
  invalidatePixelProbe() {}, scheduleScaleStrip() {}, clearRoi() {},
  refreshTiles() {}, renderZSlider() {}, renderTimeLine() {}, refreshHistograms() {},
  syncFrameScopedControls() {}, platePlaneNote: () => "z=1", renderSlideInspector() {},
  annotationStore: {
    needsFrameLoad: () => true,
    switchOwner: () => { state.annReady = false; switched += 1; },
  },
});
vm.runInContext(fnSource("setTool") + fnSource("plateFrameChanged") + "\nthis.api = { setTool, plateFrameChanged };", context);
const outcomes = {};
for (const tool of ["pin", "box", "measure", "astromap", "roi"]) {
  state.annReady = true;
  state.tool = tool;
  dragCancelled = 0;
  context.api.plateFrameChanged();
  outcomes[tool] = { afterTool: state.tool, dragCancelled };
}
return { outcomes, switched };
""")
    for ann_tool in ("pin", "box", "measure", "astromap"):
        assert result["outcomes"][ann_tool]["afterTool"] is None
    assert result["outcomes"]["roi"]["afterTool"] == "roi"
    assert result["outcomes"]["roi"]["dragCancelled"] == 1
    assert result["switched"] == 5


def test_sua1_tools_block_activation_and_commits_when_unready_locked_or_quitting():
    result = _run_js(r"""
const fs = require("fs"), vm = require("vm"), path = require("path");
const source = fs.readFileSync(path.join(process.argv[1], "app.js"), "utf8").replace(/\r\n/g, "\n");
const fnSource = (name) => {
  const start = source.indexOf("function " + name + "("), end = source.indexOf("\n}\n", start);
  if (start < 0 || end < 0) throw new Error("missing: " + name);
  return source.slice(start, end) + "\n}\n";
};
const nodes = new Map();
const $ = (id) => {
  if (!nodes.has(id)) {
    nodes.set(id, {
      id, hidden: true, style: {}, handlers: {}, textContent: "", title: "",
      classList: { add() {}, remove() {}, toggle() {} },
      addEventListener(kind, fn) { this.handlers[kind] = fn; },
      setPointerCapture() {}, releasePointerCapture() {},
    });
  }
  return nodes.get(id);
};
let ready = false, locked = false;
const added = [];
const state = {
  info: { width: 500, height: 500, nd2Export: true },
  plate: { focus: 0 }, tool: null, roi: null, tempLine: null, quitPreparing: false,
  landmark: { active: false },
  viewer: { addHandler() {}, setMouseNavEnabled() {} },
  windows: { region: { isHidden: () => true, open() {} }, astromap: { isHidden: () => true } },
};
const context = vm.createContext({
  state, $, Math, Object, Boolean,
  TOOL_BTN: { roi: "roi-toggle", move: "roi-move", measure: "tool-measure", pin: "tool-pin", box: "tool-box", astromap: "astromap-draw" },
  clamp: (v, lo, hi) => Math.min(hi, Math.max(lo, v)),
  elementPoint: (ev) => ({ x: ev.x, y: ev.y }),
  imgPoint: (pt) => ({ ...pt }),
  viewerElementToImagePoint: (pt) => ({ ...pt }),
  imageToViewerElementPoint: (pt) => ({ ...pt }),
  activeFrameParams: () => ({ t: 0, p: 0, z: 0 }),
  activeFrameContext: () => ({ key: "0:0:0" }),
  annotationOwnerReady: () => ready,
  annLocked: () => locked,
  astromapReady: () => ready && !locked && !state.quitPreparing,
  frameOwnsRoi: () => true,
  positionRubber: (a, b) => ({
    x: Math.min(a.x, b.x), y: Math.min(a.y, b.y),
    w: Math.abs(b.x - a.x), h: Math.abs(b.y - a.y),
  }),
  addAnnotation: (item) => { added.push(item); return { ...item, id: "id" + added.length }; },
  addAstromapRoi: (rect) => { added.push({ type: "astromap", ...rect }); },
  openEditor() {}, setAnnStatus() {}, showToast() {}, renderAnnotations() {},
  moveRoiOverlay() {}, updateRoiPanel() {}, finishSelection() {}, clearRoi() {},
  applyRoi() {}, downloadRoi() {}, wireRoiDims() {}, wireAnnotationPanel() {}, placeLandmark() {},
});
vm.runInContext(fnSource("setTool") + fnSource("wireTools") + "\nwireTools();\nthis.setTool = setTool;", context);
const stage = $("stage");
const ev = (kind, x, y) => stage.handlers[kind]({
  x, y, button: 0, pointerId: 1, pointerType: "mouse",
  shiftKey: false, metaKey: false, ctrlKey: false, altKey: false,
  preventDefault() {}, stopPropagation() {},
});

// 1. Clicking tool buttons while annReady=false must not arm.
ready = false; locked = false; state.quitPreparing = false;
$("tool-pin").onclick();
$("tool-box").onclick();
$("tool-measure").onclick();
const armedWhenUnready = state.tool;

// 2. Even if state.tool was set before plane switch made ready=false, pointerdown disarms without starting.
state.tool = "pin";
ev("pointerdown", 20, 20);
ev("pointerup", 20, 20);
const toolAfterBlockedDown = state.tool;

// 3. Gesture started while ready=true, then destination plane load / lock happens before pointerup: commit is blocked.
for (const [tool, x2, y2, mode] of [
  ["pin", 22, 22, "unready"],
  ["box", 80, 90, "locked"],
  ["measure", 80, 90, "quitting"],
]) {
  ready = true; locked = false; state.quitPreparing = false;
  context.setTool(tool);
  ev("pointerdown", 20, 20);
  ev("pointermove", x2, y2);
  if (mode === "unready") ready = false;
  if (mode === "locked") locked = true;
  if (mode === "quitting") state.quitPreparing = true;
  ev("pointerup", x2, y2);
}
const blockedCommits = added.length;

// 4. Once ready=true and unlocked, pin/box/measure commit normally.
ready = true; locked = false; state.quitPreparing = false;
$("tool-pin").onclick();
ev("pointerdown", 30, 40);
ev("pointerup", 30, 40);

return { armedWhenUnready, toolAfterBlockedDown, blockedCommits, validCommits: added.map((a) => a.type) };
""")
    assert result["armedWhenUnready"] is None
    assert result["toolAfterBlockedDown"] is None
    assert result["blockedCommits"] == 0
    assert result["validCommits"] == ["pin"]


# ---- SUA-2: read-only directory, zero lock litter, corrupt legacy sidecar ----


def test_sua2_readonly_directory_opens_and_serves_annotations_cleanly(tmp_path):
    ro_dir = tmp_path / "readonly_mount"
    ro_dir.mkdir()
    slide = ro_dir / "specimen.png"
    Image.fromarray(np.zeros((16, 16, 3), dtype=np.uint8)).save(slide)

    os.chmod(ro_dir, 0o555)
    try:
        if os.access(ro_dir, os.W_OK):
            pytest.skip("process bypasses POSIX read-only directory permissions")
        for session in (None, create_window_session("user", tmp_path / "sessions")):
            httpd = create_server([], port=0, window_session=session)
            try:
                sid = httpd.registry.open_path(slide)
                st = httpd.registry.get(sid)
                assert st is not None and st.annotations_path is None
                base = f"http://127.0.0.1:{httpd.server_address[1]}/{httpd.token}/s/{sid}"
                with serving(httpd):
                    code_get, body_get = _http_request(f"{base}/api/annotations")
                    code_post, body_post = _http_request(
                        f"{base}/api/annotations", payload={"items": []}
                    )
                assert code_get == 200 and body_get == {"items": [], "path": None}
                assert code_post == 400 and "no annotation sidecar" in body_post["error"]
            finally:
                httpd.registry.close_all(immediate=True)
    finally:
        os.chmod(ro_dir, 0o755)


def test_sua2_confocal_get_creates_no_annotations_dir_or_lock_litter(tmp_path):
    source = tmp_path / "confocal" / "stack.czi"
    source.parent.mkdir()
    source.write_bytes(b"confocal placeholder")
    attrs = {
        "nd2wsi": {
            "source": source.name,
            "levels": [{"path": "0", "width": 128, "height": 128}],
            "plate": {
                "T": 1, "P": 1, "Z": 3, "annotationScope": "plane",
                "sites": [{"i": 0, "name": "S0"}],
            },
        }
    }
    fake_plate = SimpleNamespace(T=1, P=1, Z=3, z_home=0)
    for session in (None, create_window_session("user", tmp_path / "sessions")):
        httpd = create_server([], port=0, window_session=session)
        sid = httpd.registry.sid_for(source)
        ann0 = httpd.registry.annotation_path(source, attrs, frame=(0, 0, 0))
        httpd.registry.slides[sid] = ViewerState(
            {}, attrs, annotations_path=ann0, source_path=source, plate=fake_plate
        )
        base = f"http://127.0.0.1:{httpd.server_address[1]}/{httpd.token}/s/{sid}"
        with serving(httpd):
            for z in (0, 1, 2):
                code, body = _http_request(f"{base}/api/annotations?t=0&p=0&z={z}")
                assert code == 200 and body["items"] == []
        assert not (source.parent / "nd2wsi").exists()
        assert list(source.parent.rglob("*.window-lock")) == []


def test_sua2_corrupt_legacy_site_sidecar_never_poisons_canonical_and_recovers(tmp_path):
    source = tmp_path / "confocal" / "sample.czi"
    source.parent.mkdir()
    source.write_bytes(b"confocal placeholder")
    attrs = {
        "nd2wsi": {
            "source": source.name,
            "levels": [{"path": "0", "width": 128, "height": 128}],
            "plate": {
                "T": 1, "P": 1, "Z": 2, "annotationScope": "plane",
                "sites": [{"i": 0, "name": "S0"}],
            },
        }
    }
    legacy = plate_annotations_sidecar(source, attrs, 0)
    corrupt_bytes = b"{corrupt-261-sidecar"
    legacy.write_bytes(corrupt_bytes)
    canonical, _ = confocal_annotations_sidecar(source, attrs, (0, 0, 0))

    httpd = create_server([], port=0)
    sid = httpd.registry.sid_for(source)
    httpd.registry.slides[sid] = ViewerState(
        {}, attrs, annotations_path=None, source_path=source, plate=SimpleNamespace(T=1, P=1, Z=2, z_home=0)
    )
    base = f"http://127.0.0.1:{httpd.server_address[1]}/{httpd.token}/s/{sid}"
    with serving(httpd):
        # C4: unreadable 2.6.1 legacy site sidecar is left untouched; GET opens empty
        # at the canonical path and POST creates the canonical file.
        code_ok_empty, body_empty = _http_request(f"{base}/api/annotations?t=0&p=0&z=0")
        assert code_ok_empty == 200 and body_empty == {"items": [], "path": str(canonical)}
        assert legacy.read_bytes() == corrupt_bytes
        assert not canonical.exists()

        code_post, body_post = _http_request(
            f"{base}/api/annotations?t=0&p=0&z=0", payload={"items": [{"id": "x"}]}
        )
        assert code_post == 200 and body_post["path"] == str(canonical)
        assert legacy.read_bytes() == corrupt_bytes
        assert canonical.exists()
        assert json.loads(canonical.read_text(encoding="utf-8"))["items"] == [{"id": "x"}]

        legacy.unlink()
        code_ok, body_ok = _http_request(f"{base}/api/annotations?t=0&p=0&z=0")
        assert code_ok == 200 and len(body_ok["items"]) == 1
        assert body_ok["path"] == str(canonical)


def test_c3_concurrent_annotation_path_claims_preserve_legacy_sidecar(tmp_path):
    source = tmp_path / "sample.nd2"
    source.write_bytes(b"placeholder")
    attrs = {
        "nd2wsi": {
            "source": source.name,
            "levels": [{"path": "0", "width": 64, "height": 64}],
            "selection": {"t": 0, "p": 0, "z": "mid"},
        }
    }
    legacy = tmp_path / "sample.annotations.json"
    legacy_doc = {"items": [{"id": "legacy-1", "type": "pin", "x": 3, "y": 4}]}
    legacy.write_text(json.dumps(legacy_doc), encoding="utf-8")

    # Never overwrite an existing canonical sidecar even if legacy exists too
    canonical = annotations_sidecar(source, attrs, migrate=False)
    canonical.parent.mkdir(parents=True, exist_ok=True)
    existing_doc = {"items": [{"id": "canonical-1", "type": "pin", "x": 10, "y": 20}]}
    canonical.write_text(json.dumps(existing_doc), encoding="utf-8")
    assert annotations_sidecar(source, attrs) == canonical
    assert json.loads(canonical.read_text(encoding="utf-8")) == existing_doc

    # Clean canonical and test concurrent registry.annotation_path calls claiming legacy atomically
    canonical.unlink()
    reg = SlideRegistry()
    barrier = threading.Barrier(4)
    paths: list[Path] = []

    def _claim():
        barrier.wait(timeout=5)
        paths.append(reg.annotation_path(source, attrs))

    threads = [threading.Thread(target=_claim) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=5)
    assert paths == [canonical] * 4
    assert not legacy.exists()
    assert json.loads(canonical.read_text(encoding="utf-8")) == legacy_doc


# ---- SUA-3: plane-scoped selection matching for JSON and GeoJSON -------------


def test_sua3_plane_scoped_selection_matching_json_and_geojson():
    result = _run_js(r"""
const fs = require("fs"), vm = require("vm"), path = require("path");
const geo = require(path.join(process.argv[1], "annotation-geojson-v1.js"));
const source = fs.readFileSync(path.join(process.argv[1], "app.js"), "utf8");
const section = (a, b) => source.slice(source.indexOf(a), source.indexOf(b, source.indexOf(a)));
let currentFrame = { t: 0, p: 0, z: 2 };
const state = {
  plate: { focus: 0 },
  info: {
    name: "stack.czi", width: 200, height: 200,
    plate: { annotationScope: "plane" },
    selection: { t: 0, p: 0, z: "mid", z_resolved: 2 },
  },
  annSite: 0, annotations: [],
};
const context = vm.createContext({
  window: { Nd2AnnotationGeoJSON: geo },
  state, Number, String, Array, Error, JSON,
  activeFrameParams: () => currentFrame,
  annotationStore: { ownsFrame: () => true },
  pixelSize: () => [0.5, 0.5],
});
vm.runInContext(
  section("function annotationDocument(", "function rebuildAnnList(") +
  "\nthis.check = annotationItemsForThisSlide;",
  context
);
const tryBoth = (selection) => {
  const src = { name: "stack.czi", width: 200, height: 200 };
  const out = {};
  for (const [kind, doc, isGeo] of [
    ["json", { source: src, coordinate_space: "level-0-pixels", selection, items: [{ type: "pin", x: 5, y: 6 }] }, false],
    ["geojson", {
      type: "FeatureCollection", source: src, coordinate_space: "level-0-pixels", selection,
      features: [{ type: "Feature", geometry: { type: "Point", coordinates: [5, 6] }, properties: { name: "p" } }],
    }, true],
  ]) {
    try {
      context.check(doc, isGeo);
      out[kind] = "ok";
    } catch (err) {
      out[kind] = err.message;
    }
  }
  return out;
};

const confocalPlaneCases = {
  siteOnlyMatch: tryBoth({ p: 0 }),
  siteOnlyMismatch: tryBoth({ p: 1 }),
  omittedZMatch: tryBoth({ t: 0, p: 0 }),
  wrongT: tryBoth({ t: 1, p: 0 }),
  zResolvedMatch: tryBoth({ t: 0, p: 0, z: "mid", z_resolved: 2 }),
  zResolvedMismatch: tryBoth({ t: 0, p: 0, z: "mid", z_resolved: 4 }),
  exactPlaneMatch: tryBoth({ t: 0, p: 0, z: 2 }),
  wrongZ: tryBoth({ t: 0, p: 0, z: 1 }),
};

// Non-plate converted slide (!state.plate && !state.info.plate) MUST NOT use relaxed {p}-only matching
state.plate = null;
state.info = {
  name: "stack.czi", width: 200, height: 200,
  plate: null,
  selection: { t: 0, p: 1, z: 1 },
};
const convertedSlideCases = {
  siteOnlyRefused: tryBoth({ p: 1 }),
  omittedZRefused: tryBoth({ t: 0, p: 1 }),
  exactPlaneMatch: tryBoth({ t: 0, p: 1, z: 1 }),
};

state.info = {
  name: "stack.czi", width: 200, height: 200,
  plate: null,
  selection: { t: 0, p: 1, z: "max" },
};
convertedSlideCases.maxProjectionSiteOnlyRefused = tryBoth({ p: 1 });
convertedSlideCases.maxProjectionExactMatch = tryBoth({ t: 0, p: 1, z: "max" });

return { confocalPlaneCases, convertedSlideCases };
""")
    ok = {"json": "ok", "geojson": "ok"}
    diff = {
        "json": "annotations belong to a different T/P/Z plane",
        "geojson": "annotations belong to a different T/P/Z plane",
    }
    c = result["confocalPlaneCases"]
    assert c["siteOnlyMatch"] == ok
    assert c["siteOnlyMismatch"] == diff
    assert c["omittedZMatch"] == ok
    assert c["wrongT"] == diff
    assert c["zResolvedMatch"] == ok
    assert c["zResolvedMismatch"] == diff
    assert c["exactPlaneMatch"] == ok
    assert c["wrongZ"] == diff

    s = result["convertedSlideCases"]
    assert s["siteOnlyRefused"] == diff
    assert s["omittedZRefused"] == diff
    assert s["exactPlaneMatch"] == ok
    assert s["maxProjectionSiteOnlyRefused"] == diff
    assert s["maxProjectionExactMatch"] == ok


# ---- SUA-4a + SUA-4b: .nd2svs dir trash guard, convert hint, lock release ----


def test_sua4a_directory_named_nd2svs_is_not_trashable(tmp_path):
    store_dir = tmp_path / "custom_folder.nd2svs"
    group = zarr.open_group(store_dir, mode="w", zarr_format=2)
    group.create_array("0", data=np.zeros((1, 8, 8), dtype=np.uint16), chunks=(1, 4, 4))
    group.attrs.update({
        "multiscales": [{"datasets": [{"path": "0"}]}],
        "nd2wsi": {
            "source": "sample.nd2",
            "levels": [{"path": "0", "width": 8, "height": 8, "downsample": 1}],
        },
    })
    registry = SlideRegistry()
    try:
        sid = registry.open_path(store_dir)
        st = registry.get(sid)
        assert st is not None
        assert st.trash_path is None
        with pytest.raises(ValueError, match="no cache managed by this app"):
            registry.trash_cache(sid)
        assert store_dir.is_dir()
    finally:
        registry.close_all(immediate=True)


def test_sua4a_outdated_explicit_nondefault_selection_cache_shows_convert_flags(tmp_path, monkeypatch):
    import nd2wsi.convert as convert_mod

    source = tmp_path / "sample.nd2"
    source.write_bytes(b"source slide")
    explicit_cache = tmp_path / "explicit_plane.nd2svs"
    group = open_zarr_group(explicit_cache, prefix="store.ome.zarr", mode="a")
    try:
        group.create_array("0", data=np.zeros((1, 8, 8), dtype=np.uint16), chunks=(1, 4, 4))
        group.attrs.update({
            "multiscales": [{"datasets": [{"path": "0"}]}],
            "nd2wsi": {
                "source": source.name,
                "selection": {"t": 1, "p": 2, "z": 3},
                "levels": [{"path": "0", "width": 8, "height": 8, "downsample": 1}],
            },
        })
    finally:
        group.store.close()
    manifest = {
        "format": "nd2wsi-cache/5",
        "kind": "full",
        "complete": True,
        "generation": "old-gen",
        "source": {**quick_fingerprint(source), "relative_path": source.name},
        "selection": {"t": 1, "p": 2, "z": 3, "z_resolved": 3},
    }
    with SQLiteStore(explicit_cache) as store:
        store.write_bytes("manifest.json", json.dumps(manifest).encode("utf-8"))

    monkeypatch.setattr(convert_mod, "_store_matches_source_semantics", lambda *a, **kw: False)
    registry = SlideRegistry()
    try:
        with pytest.raises(ValueError) as exc:
            registry.add_store(explicit_cache, source_path=source)
        msg = str(exc.value)
        assert "nd2wsi convert" in msg
        assert "--position 2" in msg
        assert "--t 1" in msg
        assert "--z 3" in msg
    finally:
        registry.close_all(immediate=True)


def test_sua4b_add_store_rebuild_does_not_block_concurrent_listing_and_closes_race_loser(tmp_path, monkeypatch):
    import nd2wsi.convert as convert_mod
    from nd2wsi.reader import PlaneSelection

    source = tmp_path / "sample.nd2"
    source.write_bytes(b"source slide")
    sel = PlaneSelection()
    managed_cache = cache_container(source, sel)
    managed_cache.parent.mkdir(parents=True, exist_ok=True)

    def _write_cache(gen: str) -> None:
        group = open_zarr_group(managed_cache, prefix="store.ome.zarr", mode="w")
        try:
            group.create_array("0", data=np.zeros((1, 8, 8), dtype=np.uint16), chunks=(1, 4, 4))
            group.attrs.update({
                "multiscales": [{"datasets": [{"path": "0"}]}],
                "nd2wsi": {
                    "source": source.name,
                    "selection": {"t": 0, "p": 0, "z": "mid"},
                    "levels": [{"path": "0", "width": 8, "height": 8, "downsample": 1}],
                },
            })
        finally:
            group.store.close()
        manifest = {
            "format": "nd2wsi-cache/5",
            "kind": "full",
            "complete": True,
            "generation": gen,
            "source": {**quick_fingerprint(source), "relative_path": os.path.relpath(source, managed_cache.parent)},
            "selection": {"t": 0, "p": 0, "z": "mid", "z_resolved": 0},
        }
        with SQLiteStore(managed_cache) as store:
            store.write_bytes("manifest.json", json.dumps(manifest).encode("utf-8"))

    _write_cache("stale-gen")
    rebuild_entered = threading.Event()
    allow_rebuild = threading.Event()

    def _slow_ensure_cache(*args, **kwargs):
        rebuild_entered.set()
        assert allow_rebuild.wait(timeout=10)
        _write_cache("rebuilt-gen")
        return managed_cache

    monkeypatch.setattr(convert_mod, "_store_matches_source_semantics", lambda *a, **kw: False)
    monkeypatch.setattr(convert_mod, "ensure_cache", _slow_ensure_cache)

    registry = SlideRegistry()
    errors: list[BaseException] = []
    results: list[str] = []

    def _worker():
        try:
            results.append(registry.add_store(managed_cache, source_path=source))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=_worker)
    worker.start()
    try:
        assert rebuild_entered.wait(timeout=5)
        t0 = time.monotonic()
        listing = registry.listing()
        elapsed = time.monotonic() - t0
        assert listing == []
        assert elapsed < 0.5, f"registry.listing() blocked on _lock for {elapsed:.2f}s"
    finally:
        allow_rebuild.set()
        worker.join(timeout=10)

    try:
        assert not errors
        assert len(results) == 1
        assert registry.get(results[0]).generation == "rebuilt-gen"

        # Now test two threads racing through open_store for a fresh sid: loser's root must close.
        registry.close_all(immediate=True)
        monkeypatch.setattr(convert_mod, "_store_matches_source_semantics", lambda *a, **kw: True)
        barrier = threading.Barrier(2)
        opened_roots = []
        real_open_store = convert_mod.open_store

        def _racing_open_store(path):
            root, attrs = real_open_store(path)
            opened_roots.append(root)
            barrier.wait(timeout=5)
            return root, attrs

        monkeypatch.setattr(convert_mod, "open_store", _racing_open_store)
        race_sids: list[str] = []
        threads = [
            threading.Thread(target=lambda: race_sids.append(registry.add_store(managed_cache, source_path=source)))
            for _ in range(2)
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=10)
        assert len(race_sids) == 2 and race_sids[0] == race_sids[1]
        assert len(opened_roots) == 2
        closed_flags = [r.store._closed for r in opened_roots]
        assert sorted(closed_flags) == [False, True]
    finally:
        registry.close_all(immediate=True)


# ---- SUA-5: NaN/Infinity rejection + CLI POST vs SessionFileLock 409 ---------


def test_sua5_nonfinite_json_rejected_and_allow_nan_false(tmp_path):
    source = tmp_path / "sample.nd2"
    source.write_bytes(b"placeholder")
    attrs = {
        "nd2wsi": {
            "source": source.name,
            "levels": [{"path": "0", "width": 64, "height": 64}],
            "selection": {"t": 0, "p": 0, "z": "mid"},
        }
    }
    sidecar = annotations_sidecar(source, attrs)
    httpd = create_server([], port=0)
    sid = httpd.registry.sid_for(source)
    httpd.registry.slides[sid] = ViewerState({}, attrs, annotations_path=sidecar, source_path=source)
    base = f"http://127.0.0.1:{httpd.server_address[1]}/{httpd.token}"
    with serving(httpd):
        for constant in ("NaN", "Infinity", "-Infinity"):
            raw = f'{{"items": [{{"id": "a", "type": "pin", "x": {constant}, "y": 1}}]}}'.encode()
            code, body = _http_request(f"{base}/s/{sid}/api/annotations", payload=raw)
            assert code == 400 and "non-finite" in body["error"]
            assert not sidecar.exists()

            open_raw = f'{{"path": "x", "extra": {constant}}}'.encode()
            code_open, body_open = _http_request(f"{base}/api/open", payload=open_raw)
            assert code_open == 400 and "non-finite" in body_open["error"]

        # Non-finite server-side calibration float in _annotations_post -> 400 via allow_nan=False
        httpd.registry.slides[sid].attrs["nd2wsi"]["pixel_size_um"] = [float("nan"), 0.5]
        code_nan_meta, _ = _http_request(
            f"{base}/s/{sid}/api/annotations", payload={"items": [{"id": "ok"}]}
        )
        assert code_nan_meta == 400
        assert not sidecar.exists()

    # AnnotationWorkspace.write also enforces allow_nan=False
    session = create_window_session("user", tmp_path / "sessions")
    workspace = AnnotationWorkspace(session)
    ws_target = tmp_path / "ws_annotations.json"
    _, rev = workspace.read(ws_target)
    with pytest.raises(ValueError):
        workspace.write(ws_target, {"items": [{"x": float("inf")}]}, rev)
    assert not ws_target.exists()


def test_sua5_cli_annotations_post_returns_409_when_window_lock_is_held(tmp_path, monkeypatch):
    monkeypatch.setattr(annotation_workspace, "_LOCK_TIMEOUT", 0.05)
    source = tmp_path / "sample.nd2"
    source.write_bytes(b"placeholder")
    attrs = {
        "nd2wsi": {
            "source": source.name,
            "levels": [{"path": "0", "width": 64, "height": 64}],
            "selection": {"t": 0, "p": 0, "z": "mid"},
        }
    }
    sidecar = annotations_sidecar(source, attrs)
    original = json.dumps({"items": [{"id": "window-owned"}]}).encode("utf-8")
    sidecar.write_bytes(original)

    httpd = create_server([], port=0)
    sid = httpd.registry.sid_for(source)
    httpd.registry.slides[sid] = ViewerState({}, attrs, annotations_path=sidecar, source_path=source)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/{httpd.token}/s/{sid}/api/annotations"

    lock = SessionFileLock(sidecar.with_name("." + sidecar.name + ".window-lock"))
    with serving(httpd):
        lock.acquire(timeout=1.0)
        try:
            code, body = _http_request(url, payload={"items": [{"id": "cli-clobber"}]})
            assert code == 409
            assert body == {"error": "annotations are open in another window"}
            assert sidecar.read_bytes() == original
        finally:
            lock.release()

        code_after, _ = _http_request(url, payload={"items": [{"id": "cli-after-release"}]})
        assert code_after == 200
        assert json.loads(sidecar.read_text(encoding="utf-8"))["items"] == [{"id": "cli-after-release"}]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions, flock and symlinks")
@pytest.mark.parametrize("failure", ["unopenable", "no-flock", "symlink"])
def test_b5_cli_save_does_not_depend_on_an_unusable_window_lock(tmp_path, monkeypatch, failure):
    import errno

    from nd2wsi import session_lock

    if failure == "unopenable" and os.geteuid() == 0:
        pytest.skip("root opens any file")
    source = tmp_path / "sample.nd2"
    source.write_bytes(b"placeholder")
    attrs = {
        "nd2wsi": {
            "source": source.name,
            "levels": [{"path": "0", "width": 64, "height": 64}],
            "selection": {"t": 0, "p": 0, "z": "mid"},
        }
    }
    sidecar = annotations_sidecar(source, attrs)
    sidecar.write_text('{"items": []}', encoding="utf-8")
    lock = sidecar.with_name("." + sidecar.name + ".window-lock")
    held = SessionFileLock(lock)
    held.acquire(timeout=1)
    held.release()
    if failure == "unopenable":
        os.chmod(lock, 0)
    elif failure == "no-flock":
        real = session_lock._kernel_lock

        def unsupported(fd, *, unlock=False):
            if unlock:
                return real(fd, unlock=True)
            raise OSError(errno.ENOTSUP, os.strerror(errno.ENOTSUP))

        monkeypatch.setattr(session_lock, "_kernel_lock", unsupported)
    else:
        target = tmp_path / "elsewhere"
        target.write_text("", encoding="utf-8")
        lock.unlink()
        lock.symlink_to(target)

    httpd = create_server([], port=0)
    sid = httpd.registry.sid_for(source)
    httpd.registry.slides[sid] = ViewerState({}, attrs, annotations_path=sidecar, source_path=source)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/{httpd.token}/s/{sid}/api/annotations"
    try:
        with serving(httpd):
            code, body = _http_request(url, payload={"items": [{"id": "saved"}]})
    finally:
        if failure == "unopenable":
            os.chmod(lock, 0o600)
    assert code == 200, body
    assert json.loads(sidecar.read_text(encoding="utf-8"))["items"] == [{"id": "saved"}]


def test_b5_cli_write_timeout_is_not_reported_as_window_contention(tmp_path, monkeypatch):
    import errno

    source = tmp_path / "sample.nd2"
    source.write_bytes(b"placeholder")
    attrs = {
        "nd2wsi": {
            "source": source.name,
            "levels": [{"path": "0", "width": 64, "height": 64}],
            "selection": {"t": 0, "p": 0, "z": "mid"},
        }
    }
    sidecar = annotations_sidecar(source, attrs)

    def timed_out(_fd):
        raise OSError(errno.ETIMEDOUT, os.strerror(errno.ETIMEDOUT))

    monkeypatch.setattr(annotation_workspace.os, "fsync", timed_out)
    httpd = create_server([], port=0)
    sid = httpd.registry.sid_for(source)
    httpd.registry.slides[sid] = ViewerState({}, attrs, annotations_path=sidecar, source_path=source)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/{httpd.token}/s/{sid}/api/annotations"
    with serving(httpd):
        code, body = _http_request(url, payload={"items": [{"id": "x"}]})
    assert code == 500 and body["error"].startswith(f"could not save {sidecar.name}")


@pytest.mark.parametrize("role", ["user", "agent"])
def test_b5_window_save_keeps_a_preserved_non_finite_extension_value(tmp_path, role):
    source = tmp_path / "research" / "sample.nd2"
    source.parent.mkdir()
    source.write_bytes(b"placeholder")
    attrs = {
        "nd2wsi": {
            "source": source.name,
            "levels": [{"path": "0", "width": 64, "height": 64}],
            "selection": {"t": 0, "p": 0, "z": "mid"},
        }
    }
    canonical = annotations_sidecar(source, attrs)
    canonical.write_bytes(b'{"items": [], "custom_metadata": {"mean": NaN, "hi": Infinity}}')
    session = create_window_session(role, tmp_path / "sessions")
    httpd = create_server([], port=0, window_session=session)
    path = httpd.registry.annotation_path(source, attrs)
    sid = httpd.registry.sid_for(source)
    httpd.registry.slides[sid] = ViewerState({}, attrs, annotations_path=path, source_path=source)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/{httpd.token}/s/{sid}/api/annotations"
    with serving(httpd):
        code, first = _http_request(url)
        assert code == 200
        code, saved = _http_request(url, payload={"items": [{"id": "new"}], "expected_revision": first["revision"]})
        assert code == 200, saved
        code, _ = _http_request(url, payload={"items": [{"id": "bad", "x": 1e999}], "expected_revision": saved["revision"]})
        assert code == 400
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["items"] == [{"id": "new"}]
    assert written["custom_metadata"]["hi"] == float("inf")


def test_b5c_inspect_sanitizes_nonfinite_objective_and_json_handles_internal_nan(tmp_path):
    source = tmp_path / "sample.nd2"
    source.write_bytes(b"placeholder")
    attrs = {
        "nd2wsi": {
            "source": source.name,
            "tile": 512,
            "dtype": "uint16",
            "rgb": False,
            "levels": [{"path": "0", "width": 64, "height": 64}],
            "selection": {"t": 0, "p": 0, "z": "mid"},
            "objective_magnification": float("inf"),
        },
        "omero": {"channels": [{"label": "DAPI", "color": "0000FF", "window": {"start": 0, "end": 100}}]},
    }
    httpd = create_server([], port=0)
    sid = httpd.registry.sid_for(source)
    httpd.registry.slides[sid] = ViewerState({}, attrs, source_path=source)
    base = f"http://127.0.0.1:{httpd.server_address[1]}/{httpd.token}/s/{sid}"
    with serving(httpd):
        code, body = _http_request(f"{base}/api/inspect")
        assert code == 200, body
        assert body["objective"] is None
        assert body["objective_magnification"] is None

        # _json catches non-finite values inside any internal payload with fixed 500 error
        httpd.registry.slides[sid].attrs["nd2wsi"]["pixel_size_um"] = [float("nan"), 0.5]
        code_bad, body_bad = _http_request(f"{base}/api/inspect")
        assert code_bad == 500
        assert body_bad == {"error": "internal server error: non-finite value in response"}


@pytest.mark.parametrize("with_session", [False, True])
def test_c9_annotations_get_rejects_mismatched_source_dimensions(tmp_path, with_session):
    source = tmp_path / "mosaic.lif"
    source.write_bytes(b"placeholder")
    attrs = {
        "nd2wsi": {
            "source": source.name,
            "levels": [{"path": "0", "width": 2048, "height": 2048}],
            "selection": {"t": 0, "p": 0, "z": "mid"},
        }
    }
    session = create_window_session("user", tmp_path / "sessions") if with_session else None
    httpd = create_server([], port=0, window_session=session)
    path = httpd.registry.annotation_path(source, attrs)
    path.write_text(
        json.dumps({"items": [], "source": {"name": source.name, "width": 512, "height": 512}}),
        encoding="utf-8",
    )
    sid = httpd.registry.sid_for(source)
    httpd.registry.slides[sid] = ViewerState({}, attrs, annotations_path=path, source_path=source)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/{httpd.token}/s/{sid}/api/annotations"
    with serving(httpd):
        code, body = _http_request(url)
    assert code == 500
    assert "these annotations were drawn on a 512 x 512 image; this image is 2048 x 2048" in body["error"]


# ---- SUA-6: refreshShadingPyramid channel.label + LutSettingsStore refresh ---


def test_sua6_refresh_shading_pyramid_checks_channel_label():
    result = _run_js(r"""
const fs = require("fs"), vm = require("vm"), path = require("path");
const source = fs.readFileSync(path.join(process.argv[1], "app.js"), "utf8").replace(/\r\n/g, "\n");
const start = source.indexOf("async function refreshShadingPyramid(");
const end = source.indexOf("\n}\n", start);
const fnCode = source.slice(start, end) + "\n}\n";

let nextInfo = null;
const state = {
  info: {
    width: 256, height: 256, kind: "slide", generation: "g1",
    channels: [{ label: "DAPI", window: { start: 0, end: 100 } }, { label: "FITC", window: { start: 0, end: 200 } }],
  },
  luts: [null, null],
  lutAutoPending: [true, true],
  inspect: { stale: true },
};
const context = vm.createContext({
  state, Array, Object, Error,
  window: { Nd2LutTransfer: { currentLut: (ch) => ({ lo: ch.window.start, hi: ch.window.end, gamma: 1 }) } },
  activeFrameContext: () => ({ key: "slide" }),
  fetch: async () => ({ ok: true, status: 200, json: async () => nextInfo }),
  frameRequests: { pixels: { invalidate() {} }, histograms: { schedule() {} } },
  $: () => ({ replaceChildren() {} }),
  buildLevelLamps() {}, updateScaleStrip() {},
  reopenPreservingView: async () => true,
});
vm.runInContext(fnCode + "\nthis.refresh = refreshShadingPyramid;", context);

nextInfo = {
  width: 256, height: 256, kind: "slide", generation: "g2",
  channels: [{ label: "DAPI", window: { start: 0, end: 120 } }, { label: "TRITC", window: { start: 0, end: 200 } }],
};
let mismatchError = null;
try {
  await context.refresh("g2", { key: "slide" });
} catch (err) {
  mismatchError = err.message;
}

nextInfo = {
  width: 256, height: 256, kind: "slide", generation: "g3",
  channels: [{ label: "DAPI", window: { start: 0, end: 120 } }, { label: "FITC", window: { start: 0, end: 250 } }],
};
await context.refresh("g3", { key: "slide" });
return { mismatchError, updatedGen: state.info.generation, end0: state.info.channels[0].window.end };
""")
    assert result["mismatchError"] == "The rebuilt pyramid does not match this slide's geometry or channels"
    assert result["updatedGen"] == "g3"
    assert result["end0"] == 120


def test_sua6_adopt_pyramid_refreshes_lut_settings_store_in_place(tmp_path, monkeypatch):
    monkeypatch.setenv("ND2WSI_LUT_SETTINGS_ROOT", str(tmp_path / "luts"))
    source = tmp_path / "slide.nd2"
    source.write_bytes(b"immutable source bytes")

    def _attrs(end_val: int, dtype: str = "uint16") -> dict:
        return {
            "nd2wsi": {
                "source": source.name,
                "dtype": dtype,
                "rgb": False,
                "selection": {"t": 0, "p": 0, "z": "mid"},
                "levels": [{"path": "0", "width": 128, "height": 128, "downsample": 1}],
            },
            "omero": {
                "channels": [{"label": "DAPI", "window": {"start": 10, "end": end_val}}],
            },
        }

    initial_attrs = _attrs(100)
    lut_store = LutSettingsStore(source, initial_attrs)
    st = ViewerState(
        {},
        initial_attrs,
        source_path=source,
        generation="g1",
        manifest={"generation": "g1"},
    )
    st._lut_settings_store = lut_store

    rebuilt_attrs = _attrs(4095)
    st._adopt_pyramid({}, rebuilt_attrs, {"generation": "g2"})

    # Invariant: object reference on ViewerState is preserved
    assert st._lut_settings_store is lut_store
    assert lut_store.identity["context"]["channels"] == [
        {"label": "DAPI", "window": {"start": 10, "end": 4095}}
    ]
    payload = {"format": LUT_FORMAT, "luts": [{"lo": 10, "hi": 10.5, "gamma": 1}]}
    # Integer range < 1 is rejected unless exact default window; updated default (10..4095) rejects 10..100 default
    with pytest.raises(ValueError, match="at least one intensity unit"):
        lut_store.write(payload)
    # Valid post-shading LUT persists and reads back cleanly without identity mismatch
    valid = {"format": LUT_FORMAT, "luts": [{"lo": 10, "hi": 4095, "gamma": 1}]}
    assert lut_store.write(valid) == {"ok": True}
    restored = lut_store.read()
    assert restored == {"ok": True, "available": True, "settings": valid}


# ---- SUA-7: strict Host header validation in _gate() -------------------------


def test_sua7_host_header_strict_loopback_validation():
    httpd = create_server([], port=0)
    port = httpd.server_address[1]
    path = f"/{httpd.token}/api/slides"

    def _raw_get(hosts: list[str]) -> int:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.putrequest("GET", path, skip_host=True)
            for h in hosts:
                conn.putheader("Host", h)
            conn.endheaders()
            resp = conn.getresponse()
            resp.read()
            return resp.status
        finally:
            conn.close()

    with serving(httpd):
        for bad in (
            [],
            [""],
            ["   "],
            [":8080"],
            [f":{port}"],
            ["127.0.0.1:evil"],
            ["127.0.0.1:"],
            ["127.0.0.1:80:90"],
            ["[::1]:bad"],
            ["[::1]:"],
            ["[::1"],
            ["[::1]extra"],
            ["evil.example:80"],
            ["127.0.0.1", "127.0.0.1"],
            ["127.0.0.1", "evil.example"],
        ):
            assert _raw_get(bad) == 404, f"expected 404 for Host={bad!r}"

        for good in (
            ["127.0.0.1"],
            [f"127.0.0.1:{port}"],
            ["localhost"],
            [f"LOCALHOST:{port}"],
            ["[::1]"],
            [f"[::1]:{port}"],
        ):
            assert _raw_get(good) == 200, f"expected 200 for Host={good!r}"

        # C6: HTTP/0.9 request line has no HTTP headers object with .get_all -> bare 404, no AttributeError leak
        import socket

        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(f"GET /{httpd.token}/api/slides\r\n".encode("ascii"))
            sock.shutdown(socket.SHUT_WR)
            raw_reply = b""
            while chunk := sock.recv(4096):
                raw_reply += chunk
        assert json.loads(raw_reply.decode("utf-8")) == {"error": "not found"}
        assert b"AttributeError" not in raw_reply


# ---- SW-4: packaging/build_windows_local.ps1 SDPC codec & archive filter -----


def test_sw4_build_windows_local_builds_sdpc_codec_and_filters_portable_zip():
    import importlib.util
    import re
    import subprocess
    import sys

    script = (ROOT / "packaging" / "build_windows_local.ps1").read_text(encoding="utf-8")

    assert "'ND2WSI_SDPC_CODEC_MANIFEST'" in script
    preflight_idx = script.index("'packaging/build_sdpc_codec.py', '--preflight'")
    pytest_unit_idx = script.index("Invoke-Checked $python @('-m', 'pytest', 'tests', '-q', '-m', 'not realdata')")
    pytest_real_idx = script.index("Invoke-Checked $python @('-m', 'pytest', 'tests', '-q', '-m', 'realdata')")
    build_codec_idx = script.index("'packaging/build_sdpc_codec.py', '--output'")
    verify_wheel_idx = script.index("'packaging/verify_sdpc_codec.py', '--manifest', $codecManifest, '--wheel-only'")
    install_wheel_idx = script.index("'--no-deps', '--reinstall', $codecWheel")
    verify_runtime_idx = script.index("Invoke-Checked $python @('packaging/verify_sdpc_codec.py', '--manifest', $codecManifest)")
    build_win_idx = script.index("'packaging/build_windows.py'")

    assert (
        preflight_idx
        < pytest_unit_idx
        < pytest_real_idx
        < build_codec_idx
        < verify_wheel_idx
        < install_wheel_idx
        < verify_runtime_idx
        < build_win_idx
    )

    # Archive check must filter to viewer portable zip, not '*.zip', so sdpc-codec-sources-windows-x64.zip never collides
    assert "Get-ChildItem -LiteralPath $output -Filter 'nd2wsi-viewer-*-windows-x64.zip'" in script
    assert "Get-ChildItem -LiteralPath $output -Filter '*.zip'" not in script
    assert "sdpc-codec-sources-windows-x64.zip" in script
    assert "[string]$OutputZip" in script
    assert script.count("'packaging/build_sdpc_codec.py', '--output'") == 1

    # CLI contract cross-check: every --flag passed to packaging/*.py must exist in --help
    for match in re.finditer(r"@\('packaging/([a-z_]+\.py)'([^)]*)\)", script):
        py_script = match.group(1)
        used = set(re.findall(r"'(--[a-z0-9-]+)'", match.group(2)))
        help_out = subprocess.run(
            [sys.executable, f"packaging/{py_script}", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        known = set(re.findall(r"(?<![\w-])(--[a-z0-9][a-z0-9-]*)", help_out))
        assert used <= known, f"{py_script}: unknown flags {used - known}"

    # Every $env:VAR assignment must be in $environmentBefore and restored in finally
    saved_block = re.search(r"foreach \(\$name in @\(([^)]*)\)\) \{\s*\$environmentBefore", script).group(1)
    saved_names = set(re.findall(r"'([A-Z0-9_]+)'", saved_block))
    assigned = set(re.findall(r"\$env:([A-Z0-9_]+)\s*=", script))
    assert assigned <= saved_names

    # Unit-test build_sdpc_codec.preflight_check for ARM VM, missing cl.exe, and missing MSYS2 tools
    spec = importlib.util.spec_from_file_location("build_sdpc_codec", ROOT / "packaging" / "build_sdpc_codec.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    assert builder.__name__ == "build_sdpc_codec"


def test_sw4_preflight_check_host_and_toolchain_gates(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("build_sdpc_codec", ROOT / "packaging" / "build_sdpc_codec.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)

    msys_usr_bin = tmp_path / "msys64" / "usr" / "bin"
    msys_usr_bin.mkdir(parents=True)
    bash = msys_usr_bin / "bash.exe"
    bash.write_text("", encoding="utf-8")

    # 1. Windows ARM64 (e.g. Windows 11 ARM VM) refuses immediately with ND2WSI_SDPC_CODEC_MANIFEST hint
    monkeypatch.setattr(builder.platform, "system", lambda: "Windows")
    monkeypatch.setattr(builder.platform, "machine", lambda: "ARM64")
    with pytest.raises(SystemExit, match="ND2WSI_SDPC_CODEC_MANIFEST"):
        builder.preflight_check(str(bash))

    # 2. Windows x64 without make.exe refuses
    monkeypatch.setattr(builder.platform, "machine", lambda: "AMD64")
    with pytest.raises(SystemExit, match="Install make"):
        builder.preflight_check(str(bash))

    # 3. Windows x64 without diffutils (cmp/diff) refuses
    (msys_usr_bin / "make.exe").write_text("", encoding="utf-8")
    with pytest.raises(SystemExit, match="Install diffutils"):
        builder.preflight_check(str(bash))

    # 4. Windows x64 without cl.exe refuses
    (msys_usr_bin / "diff.exe").write_text("", encoding="utf-8")
    monkeypatch.setattr(builder.shutil, "which", lambda _cmd: None)
    with pytest.raises(SystemExit, match="cl\\.exe missing"):
        builder.preflight_check(str(bash))

    # 5. Windows x64 with full toolchain passes
    monkeypatch.setattr(builder.shutil, "which", lambda cmd: r"C:\VS\cl.exe" if cmd == "cl.exe" else None)
    assert builder.preflight_check(str(bash)) == ("Windows", "amd64")
