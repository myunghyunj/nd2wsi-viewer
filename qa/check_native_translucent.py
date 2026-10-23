"""Check tracing overlays in one new private Agent window, without OS input."""

from __future__ import annotations

import hashlib
import json
import os
import time
import traceback
from pathlib import Path

os.environ["ND2WSI_DISABLE_UPDATES"] = "1"
os.environ["ND2WSI_APP_NAME"] = "nd2wsi-viewer Translucent QA"

import numpy as np  # noqa: E402
import webview  # noqa: E402
import zarr  # noqa: E402
from PIL import Image  # noqa: E402

import nd2wsi  # noqa: E402
from nd2wsi import app  # noqa: E402
from nd2wsi.convert import build_group_attrs  # noqa: E402
from nd2wsi.reader import ChannelInfo, PlaneSource  # noqa: E402
from nd2wsi.translucent_smoke import (  # noqa: E402
    bridge_result,
    camera,
    check_camera,
    click_shell_cloud,
    exercise_translucent_viewer,
    pane_eval,
    save_cocoa_snapshot,
    wait_for,
)
from nd2wsi.window_sessions import create_window_session  # noqa: E402

qa = Path(os.environ["ASTROMAP_QA_ROOT"]).resolve()
report_path = qa / os.environ.get("ASTROMAP_QA_REPORT", "translucent-native-check.json")
session = create_window_session("agent", qa / "sessions")
api, parent = app.create_app_window(None, window_session=session)
report = {"ok": False, "module": nd2wsi.__file__, "session": session.as_dict()}


def write_report():
    report["exports_report"] = str(session.exports_root / "translucent-native-report.json")
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    (session.exports_root / "translucent-native-report.json").write_text(payload, encoding="utf-8")
    report_path.write_text(payload, encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)


def check(value, message):
    if not value:
        raise AssertionError(message)


def tree_digest(path):
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if item.is_file():
            digest.update(str(item.relative_to(path)).encode())
            digest.update(item.read_bytes())
    return digest.hexdigest()


def synthetic_store():
    """Build only private QA pixels; never open/rebuild a research-data cache."""
    yy, xx = np.indices((1024, 1536))
    raw = np.stack(((xx * 3 + yy) % 4096, (yy * 5 + 700) % 4096)).astype(np.uint16)
    source = PlaneSource(data=None, dtype=raw.dtype, shape=raw.shape, rgb=False,
                         channels=[ChannelInfo("QA FITC", (0, 255, 80)), ChannelInfo("QA DAPI", (60, 100, 255))],
                         pixel_size_um=(.5, .5), source_name="translucent-QA.nd2",
                         selection={"t": 0, "p": 0, "z": 0})
    levels = [raw[:, ::factor, ::factor] for factor in (1, 2, 4, 8)]
    attrs = build_group_attrs(source, [data.shape[1:] for data in levels], 256,
                              [dict(start=0, end=4095, min=0, max=4095)] * 2)
    store = session.exports_root / "synthetic.ome.zarr"
    group = zarr.open_group(str(store), mode="w")
    for index, data in enumerate(levels):
        group.create_array(str(index), data=data, chunks=(1, 256, 256))
    group.attrs.update(attrs)
    return store


def shell_toolbar_checks():
    """Check the real shell controls and save both native disclosure states."""
    state_script = """(() => {
      const group=document.getElementById('shell-tools');
      const toggle=document.getElementById('shell-tools-toggle');
      const chevron=toggle?.querySelector('.tools-chevron');
      if(!group || !toggle || !chevron) return null;
      const ids=['astromap-toggle','shading-correction','compare-toggle',
        'translucent-toggle','info-toggle'];
      return {hidden:group.hidden,expanded:toggle.getAttribute('aria-expanded'),
        chevron:getComputedStyle(chevron).maskImage || getComputedStyle(chevron).webkitMaskImage,
        active:document.querySelector('#frames iframe.active')?.src,
        buttons:ids.map(id=>{const b=document.getElementById(id),r=b.getBoundingClientRect();
          return {id,inGroup:group.contains(b),disabled:b.disabled,
            rect:[r.x,r.y,r.width,r.height]};})};
    })()"""

    def capture(name):
        bridge_result(parent, "new Promise(resolve => requestAnimationFrame(() => "
                              "requestAnimationFrame(() => resolve(true))))")
        time.sleep(.2)
        path = save_cocoa_snapshot(parent, session.exports_root / f"toolbar-{name}-native.png")
        geometry = parent.evaluate_js("({width:innerWidth,bar:document.getElementById('tabbar').getBoundingClientRect().bottom})")
        top_path = session.exports_root / f"toolbar-{name}-top.png"
        with Image.open(path) as image:
            scale = image.width / geometry["width"]
            image.crop((0, 0, image.width, min(image.height, round((geometry["bar"] + 105) * scale)))).save(top_path)
            return {"path": str(path), "top": str(top_path), "size": [image.width, image.height]}

    initial = wait_for(lambda: parent.evaluate_js(state_script), description="shell tool disclosure")
    check(initial["hidden"] and initial["expanded"] == "false", "Top tools are not initially collapsed")
    check("angle-left.svg" in initial["chevron"], f"Collapsed disclosure is not the requested angle-left: {initial}")
    check(all(button["rect"][2] == 0 for button in initial["buttons"]),
          "Collapsed group still occupies visible button space")
    check(pane_eval(parent, "!document.getElementById('tb-translucent') && !document.getElementById('tb-info')"),
          "Cloud or Info remains in the lower toolbar")
    wait_for(lambda: pane_eval(parent, "state.viewer.world.getItemAt(0).getFullyLoaded()"), description="parent screenshot tiles")
    collapsed_snapshot = capture("collapsed")
    parent.evaluate_js("document.getElementById('shell-tools-toggle').click(); true")
    expanded = wait_for(lambda: (value if not value["hidden"] and value["expanded"] == "true" else None)
                        if (value := parent.evaluate_js(state_script)) else None,
                        description="expanded shell tools")
    check("angle-right.svg" in expanded["chevron"], f"Expanded disclosure is not the requested angle-right: {expanded}")
    check(all(button["inGroup"] and button["rect"][2] > 0 and button["rect"][3] > 0
              for button in expanded["buttons"]), "A top control is missing from the expanded group")
    centers = [button["rect"][1] + button["rect"][3] / 2 for button in expanded["buttons"]]
    check(max(centers) - min(centers) < 2, "Top controls do not share one horizontal line")
    check(not any(button["disabled"] for button in expanded["buttons"]
                  if button["id"] in {"translucent-toggle", "info-toggle"}),
          "Cloud or Info remains disabled after source load")
    expanded_snapshot = capture("expanded")
    check(pane_eval(parent, "state.windows.info.isHidden()"), "Info should initially be closed")
    parent.evaluate_js("document.getElementById('info-toggle').click(); true")
    wait_for(lambda: pane_eval(parent, "!state.windows.info.isHidden()"), description="shell Info open")
    check(parent.evaluate_js("document.querySelector('#frames iframe.active').src") == expanded["active"],
          "Info changed the active pane")
    info_snapshot = capture("info-open")
    parent.evaluate_js("document.getElementById('info-toggle').click(); true")
    wait_for(lambda: pane_eval(parent, "state.windows.info.isHidden()"), description="shell Info close")
    parent.evaluate_js("document.getElementById('shell-tools-toggle').click(); true")
    wait_for(lambda: parent.evaluate_js("document.getElementById('shell-tools').hidden && "
                                       "document.getElementById('shell-tools-toggle').getAttribute('aria-expanded')==='false'"),
             description="shell tools collapse roundtrip")
    return {"collapsed": initial, "expanded": expanded, "lower_cloud_info_absent": True,
            "info_active_pane_open_close": True, "collapse_roundtrip": True,
            "snapshots": {"collapsed": collapsed_snapshot, "expanded": expanded_snapshot, "info_open": info_snapshot}}


def pair_checks(overlay, child):
    """Use the same native read and canonical JS map as real pair imports."""
    text = session.exports_root / "roi-reference.txt"
    image = session.exports_root / "roi-reference.jpg"
    text.write_text("INDEX\tX1\tY1\tX2\tY2\tNOTE\n7\t10\t20\t10\t20\tone pixel\n"
                    "19\t130\t80\t135\t85\tsecond\n", encoding="utf-8")
    Image.new("RGB", (384, 256), (30, 80, 160)).save(image)
    hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in (text, image)]
    native_read = overlay._load_roi_files([image, text])
    check(native_read.get("ok") and "text" in native_read and "rois" not in native_read,
          "Pair reader bypassed the shared AstroMapper parser")
    before = camera(child)
    accepted = child.evaluate_js("window.nd2TranslucentSetRois(" + json.dumps(native_read) + ")")
    check(accepted, "Pair import was rejected")
    state = child.evaluate_js("({rois:nd2Translucent.state.rois,index:nd2Translucent.state.roiIndex,"
                              "hidden:document.getElementById('roi-controls').hidden})")
    check(not state["hidden"] and state["index"] == 0 and len(state["rois"]) == 2,
          "ROI navigation did not appear after importing the pair")
    first = state["rois"][0]
    check([first[key] for key in ("x", "y", "w", "h")] == [40, 80, 4, 4],
          f"Inclusive single-pixel rectangle changed: {first}")
    after_first = camera(child)
    check_camera(after_first, before, center=False, description="first ROI jump")
    check(max(abs(a - b) for a, b in zip(after_first["center"], [42, 82])) < .01,
          "First ROI did not become the image center")
    child.evaluate_js("document.getElementById('roi-next').click(); true")
    after_second = camera(child)
    check_camera(after_second, before, center=False, description="next ROI jump")
    check(max(abs(a - b) for a, b in zip(after_second["center"], [532, 332])) < .01,
          "Next arrow selected the wrong ROI center")
    child.evaluate_js("document.getElementById('overlay-lock').click(); true")
    wait_for(lambda: child.evaluate_js("nd2Translucent.state.locked && !nd2Translucent.state.pending"),
             description="ROI navigation lock")
    child.evaluate_js("document.getElementById('roi-previous').click(); true")
    check_camera(camera(child), after_second, description="locked ROI navigation")
    check(not child.evaluate_js("window.nd2TranslucentSetRois(" + json.dumps(native_read) + ")"),
          "Locked overlay accepted a pair replacement")
    child.evaluate_js("document.getElementById('overlay-lock').click(); true")
    wait_for(lambda: child.evaluate_js("!nd2Translucent.state.locked && !nd2Translucent.state.pending"),
             description="ROI navigation unlock")
    check(hashes == [hashlib.sha256(path.read_bytes()).hexdigest() for path in (text, image)],
          "Pair import changed the reference files")
    return {"mapped": state["rois"], "first_camera": after_first, "next_camera": after_second,
            "inclusive_endpoints": True, "scale_preserved": True, "locked_navigation_blocked": True,
            "reference_files_unchanged": True}


def hydrate_checks():
    """Imported source regions populate navigation without moving its initial view."""
    # Modify only this fresh Agent's in-memory synthetic annotations. No save or
    # browser annotation-change event runs, and no external sidecar is touched.
    pane_eval(parent, """state.annotations=[
      {id:'qa-1',type:'box',astromap:true,astromapImportIndex:7,x:40,y:80,w:4,h:4,text:'one pixel'},
      {id:'qa-2',type:'box',astromap:true,astromapImportIndex:19,x:520,y:320,w:24,h:24,text:'second'}
    ]; state.astromap.activeId='qa-2'; true""")
    annotations = pane_eval(parent, "JSON.stringify(state.annotations)")
    expected = pane_eval(parent, "captureTranslucentView()")
    check(expected.get("roiIndex") == 1 and len(expected.get("rois", [])) == 2,
          "Source imported ROI snapshot is missing")
    click_shell_cloud(parent)
    overlay = wait_for(lambda: next(iter(api._translucent_windows.values()), None), description="hydrated overlay")
    child = overlay._window
    try:
        wait_for(lambda: child.evaluate_js("!!window.nd2Translucent?.state.ready && nd2Translucent.state.rois.length===2"),
                 description="imported ROI hydration")
        check_camera(camera(child), expected, description="hydration retains source camera")
        state = child.evaluate_js("({index:nd2Translucent.state.roiIndex,text:document.getElementById('roi-index').textContent})")
        check(state == {"index": 1, "text": "2/2"}, f"Wrong imported ROI selection: {state}")
        child.evaluate_js("document.getElementById('roi-previous').click(); true")
        check_camera(camera(child), expected, center=False, description="hydrated previous ROI scale")
        check(pane_eval(parent, "JSON.stringify(state.annotations)") == annotations,
              "Overlay modified the source's imported annotation geometry")
        return {"selected_index": 1, "initial_camera_preserved": True, "source_annotations_unchanged": True}
    finally:
        overlay.close_translucent()
        wait_for(lambda: not api._translucent_windows, description="hydrated overlay close")


def run():
    try:
        context = api.window_context()
        check(context["role"] == "agent" and context["id"] == session.id, "Wrong Agent window identity")
        wait_for(lambda: parent.evaluate_js("!!window.pywebview?.api"), description="parent bridge")
        bridged = bridge_result(parent, "pywebview.api.window_context()")
        check(bridged["role"] == "agent" and bridged["id"] == session.id, "Parent bridge identity mismatch")
        report["bridge_identity_verified"] = True
        store = synthetic_store()
        before = tree_digest(store)
        api._httpd, url = app.start_server(store, window_session=session)
        parent.load_url(url)
        wait_for(lambda: pane_eval(parent, "!!(state.info && state.annReady && state.viewer.isOpen())"),
                 description="private synthetic fluorescence source")
        report["shell_toolbar"] = shell_toolbar_checks()
        report["native_keyboard"] = app._gui_smoke_native_keyboard(api, parent, slide=True)
        check(report["native_keyboard"]["status"] == "passed", "Native keyboard smoke failed")
        pane_eval(parent, """state.channels=[1]; state.luts=[null,{lo:100,hi:3500,gamma:.7}];
          const p=state.viewer.viewport; p.setRotation(90,true); p.setFlip(true);
          p.zoomTo(p.imageToViewportZoom(.8),null,true);
          p.panTo(p.imageToViewportCoordinates(768,512),true); true""")
        result = exercise_translucent_viewer(
            api, parent, screenshot_path=session.exports_root / "translucent-native.png", extra_checks=pair_checks,
        )
        report["overlay"] = result
        check(result["ok"], result.get("error", "Overlay smoke check failed"))
        # Parent restore is native/asynchronous, but source UI remains usable.
        time.sleep(.2)
        report["hydration"] = hydrate_checks()
        check(tree_digest(store) == before, "Overlay work changed synthetic source pixels or metadata")
        report["source_store_unchanged"] = True
        report["ok"] = True
    except Exception:
        report["error"] = traceback.format_exc()
    finally:
        write_report()
        api._close_translucent_windows()
        parent.destroy()


try:
    webview.start(run, private_mode=True)
except Exception:
    report["error"] = traceback.format_exc()
    write_report()
finally:
    if api._httpd is not None:
        api.stop_server_for_update()
    session.mark_closed()
