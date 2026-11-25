"""Live overlay checks shared by private Agent QA and packaged GUI smoke tests.

All input is sent to the supplied viewer and its newly opened overlay. This
module never discovers existing applications or sends operating-system input.
"""

from __future__ import annotations

import json
import math
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path


def _check(value, message):
    if not value:
        raise AssertionError(message)


def wait_for(operation, *, timeout=30, description="condition"):
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        try:
            last = operation()
            if last:
                return last
        except Exception as exc:
            last = str(exc)
        time.sleep(.05)
    raise TimeoutError(f"Timed out waiting for {description}: {last}")


def bridge_result(window, expression, *, timeout=30):
    key = "__overlay_smoke_" + uuid.uuid4().hex
    window.evaluate_js(
        "Promise.resolve(" + expression + ").then(value=>window[" + json.dumps(key) +
        "]={value}).catch(error=>window[" + json.dumps(key) + "]={error:String(error)}); true"
    )
    result = wait_for(lambda: window.evaluate_js("window[" + json.dumps(key) + "] || null"),
                      timeout=timeout, description=expression)
    window.evaluate_js("delete window[" + json.dumps(key) + "]; true")
    if "error" in result:
        raise RuntimeError(result["error"])
    return result.get("value")


def pane_eval(parent, expression):
    """Run a diagnostic expression against the active slide pane without eval()."""
    body = expression.strip()
    if ";" in body and not body.startswith(("(()", "(function")):
        head, tail = body.rsplit(";", 1)
        tail = tail.strip()
        stmt = f"{head}; return ({tail});" if tail else f"{head}; return true;"
    else:
        stmt = f"return ({body});"
    return parent.evaluate_js(
        "(() => {"
        " const __frame = window.document.querySelector('#frames iframe.active');"
        " const __win = __frame && __frame.contentWindow;"
        " if (!__win) return null;"
        " const state = __win.nd2ViewerState;"
        " const document = __win.document;"
        " const captureTranslucentView = __win.captureTranslucentView;"
        " const applyDesiredDisplayTransform = __win.applyDesiredDisplayTransform;"
        " const Nd2Astromap = __win.Nd2Astromap;"
        " const Nd2LutTransfer = __win.Nd2LutTransfer;"
        " const loadSlideInspector = __win.loadSlideInspector;"
        " const addAnnotation = __win.addAnnotation;"
        " const renderAnnotations = __win.renderAnnotations;"
        " const refreshAstromapPanel = __win.refreshAstromapPanel;"
        " const annotationsChanged = __win.annotationsChanged;"
        " const imageToViewerElementPoint = __win.imageToViewerElementPoint;"
        " const lutParam = __win.lutParam;"
        " const pixelSize = __win.pixelSize;"
        " const astromapItems = __win.astromapItems;"
        " const Nd2ViewerGeometry = __win.Nd2ViewerGeometry;"
        " const OpenSeadragon = __win.OpenSeadragon;"
        " " + stmt +
        "})()"
    )


def click_shell_cloud(parent, *, timeout=30):
    """Use the visible shell disclosure and cloud controls, as a user would."""
    wait_for(lambda: parent.evaluate_js("""(() => {
      const group=document.getElementById('shell-tools');
      const toggle=document.getElementById('shell-tools-toggle');
      const cloud=document.getElementById('translucent-toggle');
      return !!(group && toggle && cloud && !cloud.disabled);
    })()"""), timeout=timeout, description="shell cloud control")
    parent.evaluate_js("""(() => {
      if(document.getElementById('shell-tools').hidden)
        document.getElementById('shell-tools-toggle').click();
      return true;
    })()""")
    wait_for(lambda: parent.evaluate_js("""(() => {
      const group=document.getElementById('shell-tools');
      const button=document.getElementById('translucent-toggle');
      const r=button.getBoundingClientRect();
      return !group.hidden && r.width>0 && r.height>0 && !button.disabled;
    })()"""), timeout=timeout, description="expanded shell cloud control")
    parent.evaluate_js("document.getElementById('translucent-toggle').click(); true")


def native_call(window, operation, *, timeout=30):
    complete, result = threading.Event(), {}

    def run():
        try:
            result["value"] = operation(window.native)
        except Exception as exc:
            result["error"] = exc
        finally:
            complete.set()

    if sys.platform == "darwin":
        from Foundation import NSOperationQueue

        NSOperationQueue.mainQueue().addOperationWithBlock_(run)
    elif sys.platform == "win32":
        from System import Action

        window.native.BeginInvoke(Action(run))
    else:
        raise RuntimeError("Native overlay smoke tests require macOS or Windows")
    _check(complete.wait(timeout), "native operation timed out")
    if "error" in result:
        raise result["error"]
    return result.get("value")


def native_state(window, *, timeout=30):
    def inspect(native):
        if sys.platform == "darwin":
            import AppKit

            frame = native.frame()
            return {"visible": bool(native.isVisible()), "minimized": bool(native.isMiniaturized()),
                    "topmost": native.level() >= AppKit.NSFloatingWindowLevel,
                    "opaque": bool(native.isOpaque()), "background_alpha": native.backgroundColor().alphaComponent(),
                    "movable": bool(native.isMovable()), "hides_on_deactivate": bool(native.hidesOnDeactivate()),
                    "has_parent": native.parentWindow() is not None, "style_mask": int(native.styleMask()),
                    "frame": [frame.origin.x, frame.origin.y, frame.size.width, frame.size.height]}
        scale = getattr(native, "_scale", 1)
        return {"visible": bool(native.Visible), "minimized": str(native.WindowState) == "Minimized",
                "topmost": bool(native.TopMost), "opacity": float(native.Opacity),
                "has_parent": native.Owner is not None,
                "frame": [native.Left / scale, native.Top / scale, native.Width / scale, native.Height / scale]}

    return native_call(window, inspect, timeout=timeout)


CAMERA = """(() => {
  const v=window.nd2Translucent.viewer, p=v.viewport;
  const c=p.viewportToImageCoordinates(p.getCenter(true));
  return {center:[c.x,c.y],zoom:p.viewportToImageZoom(p.getZoom(true)),
    rotation:p.getRotation(),flip:p.getFlip(),size:[innerWidth,innerHeight]};
})()"""


def camera(window):
    return window.evaluate_js(CAMERA)


def check_camera(actual, expected, *, center=True, description="camera"):
    _check(math.isclose(actual["zoom"], expected["zoom"], rel_tol=1e-6, abs_tol=1e-8),
           f"{description}: scale changed {expected['zoom']} -> {actual['zoom']}")
    if center:
        _check(max(abs(a - b) for a, b in zip(actual["center"], expected["center"])) < .01,
               f"{description}: center changed {expected['center']} -> {actual['center']}")


def wheel(window):
    return window.evaluate_js("""(() => {
      const c=window.nd2Translucent.viewer.canvas,r=c.getBoundingClientRect();
      return c.dispatchEvent(new WheelEvent('wheel',{deltaY:-100,deltaMode:0,
        clientX:r.left+r.width/2,clientY:r.top+r.height/2,bubbles:true,cancelable:true}));
    })()""")


def drag_image(window):
    return window.evaluate_js("""(() => {
      const c=window.nd2Translucent.viewer.canvas,r=c.getBoundingClientRect();
      const point=(type,x,y)=>c.dispatchEvent(new PointerEvent(type,{
        pointerId:41,pointerType:'mouse',isPrimary:true,button:0,
        buttons:type==='pointerup'?0:1,clientX:r.left+x,clientY:r.top+y,
        bubbles:true,cancelable:true}));
      point('pointerdown',r.width/2,r.height/2);
      point('pointermove',r.width/2+35,r.height/2+23);
      point('pointerup',r.width/2+35,r.height/2+23);return true;
    })()""")


def save_cocoa_snapshot(window, path, *, timeout=30):
    """Save only the supplied WKWebView, without capturing another app."""
    from AppKit import NSBitmapImageFileTypePNG, NSBitmapImageRep
    from Foundation import NSOperationQueue
    from webview.platforms.cocoa import BrowserView

    complete, errors = threading.Event(), []
    path = Path(path)

    def finish(native_image, error):
        try:
            if error is not None:
                raise RuntimeError(str(error))
            bitmap = NSBitmapImageRep.imageRepWithData_(native_image.TIFFRepresentation())
            saved = bitmap.representationUsingType_properties_(NSBitmapImageFileTypePNG, {}).writeToFile_atomically_(str(path), True)
            _check(saved, "Could not save native snapshot")
        except Exception as exc:
            errors.append(exc)
        finally:
            complete.set()

    NSOperationQueue.mainQueue().addOperationWithBlock_(
        lambda: BrowserView.instances[window.uid].webview.takeSnapshotWithConfiguration_completionHandler_(None, finish)
    )
    _check(complete.wait(timeout), "native snapshot timed out")
    if errors:
        raise errors[0]
    return path


def snapshot_cocoa(window, path, *, timeout=30, expected_opacity=.55):
    """Snapshot this WKWebView, then inspect real PNG alpha and glyph pixels."""
    from PIL import Image

    path = save_cocoa_snapshot(window, path, timeout=timeout)
    geometry = window.evaluate_js("""(() => {
      const r=document.getElementById('overlay-lock').getBoundingClientRect();
      return {width:innerWidth,height:innerHeight,lock:[r.x,r.y,r.width,r.height]};
    })()""")
    with Image.open(path) as image:
        pixels = image.convert("RGBA")
        sx, sy = image.width / geometry["width"], image.height / geometry["height"]
        middle = pixels.getpixel((image.width // 2, image.height // 2))
        corners = [pixels.getpixel(point)[3] for point in (
            (0, 0), (image.width - 1, 0), (0, image.height - 1), (image.width - 1, image.height - 1))]
        x, y, width, height = geometry["lock"]
        crop = pixels.crop((int(x * sx), int(y * sy), int((x + width) * sx), int((y + height) * sy)))
        # The glyph has its own CSS opacity, so a more opaque image underneath
        # darkens the composite RGB even when that glyph remains unchanged.
        # Recover foreground coverage from alpha instead of requiring RGB>230.
        glyph = [(r, g, b, a) for r, g, b, a in crop.getdata() if min(r, g, b) > 200 and a > 170]
        whites = len(glyph)
        background_alpha = round(expected_opacity * 255)
        foreground_alpha = ((max(pixel[3] for pixel in glyph) - background_alpha) / (255 - background_alpha)
                            if glyph and background_alpha < 255 else None)
        result = {"path": str(path), "size": [image.width, image.height], "center_rgba": middle,
                  "corner_alpha": corners, "white_lock_pixels": whites,
                  "lock_foreground_alpha": foreground_alpha}
    _check(all(alpha < 10 for alpha in corners), f"Rounded corners are not transparent: {result}")
    _check(abs(middle[3] - round(expected_opacity * 255)) <= 8,
           f"Image alpha is not approximately {expected_opacity:.0%}: {result}")
    _check(whites > 10, f"White lock glyph missing from native snapshot: {result}")
    if expected_opacity < 1:
        _check(foreground_alpha is not None and .75 <= foreground_alpha <= 1,
               f"Image opacity also reduced the lock control: {result}")
    return result


def exercise_opacity_controls(child, *, timeout=30, screenshot_path=None):
    """Use the real range input; verify its bridge and native/layer effect."""
    initial = child.evaluate_js("""(() => {
      const slider=document.getElementById('overlay-opacity');
      return {min:slider.min,max:slider.max,value:slider.value,disabled:slider.disabled,
        confirmed:nd2Translucent.state.confirmedOpacity};
    })()""")
    _check(initial == {"min": "10", "max": "100", "value": "55", "disabled": False, "confirmed": .55},
           f"Opacity slider did not start at 55% with 10–100% limits: {initial}")
    before_camera = camera(child)
    before_frame = native_state(child, timeout=timeout)["frame"]
    steps = []
    for percent in (30, 80, 55):
        child.evaluate_js("""(() => {
          const slider=document.getElementById('overlay-opacity');
          slider.value=""" + json.dumps(str(percent)) + """;
          slider.dispatchEvent(new Event('input',{bubbles:true})); return true;
        })()""")
        value = percent / 100
        wait_for(lambda: child.evaluate_js(
            f"Math.abs(nd2Translucent.state.confirmedOpacity-{value})<1e-6 && "
            f"Math.abs(nd2Translucent.state.opacity-{value})<1e-6"),
            timeout=timeout, description=f"opacity slider {percent}%")
        context = bridge_result(child, "pywebview.api.translucent_context()", timeout=timeout)
        actual = child.evaluate_js("""({layer:Number(getComputedStyle(document.getElementById('translucent-stage')).opacity),
          value:document.getElementById('overlay-opacity').value,
          label:document.getElementById('opacity-value').textContent})""")
        _check(math.isclose(context["opacity"], value), "Bridge did not persist the confirmed opacity")
        _check(actual["value"] == str(percent) and actual["label"] == f"{percent}%", "Opacity UI label disagrees with its value")
        expected_layer = 1 if sys.platform == "win32" else value
        _check(math.isclose(actual["layer"], expected_layer), "Opacity was applied to the wrong layer")
        if sys.platform == "win32":
            actual["native"] = native_state(child, timeout=timeout)["opacity"]
            _check(math.isclose(actual["native"], value), "Windows native opacity disagrees with the slider")
        elif screenshot_path is not None and percent != 55:
            wait_for(lambda: child.evaluate_js("nd2Translucent.viewer.world.getItemAt(0).getFullyLoaded()"),
                     timeout=timeout, description="opacity snapshot tiles")
            time.sleep(.2)
            path = Path(screenshot_path).with_name(f"translucent-opacity-{percent}.png")
            actual["snapshot"] = snapshot_cocoa(child, path, timeout=timeout, expected_opacity=value)
        check_camera(camera(child), before_camera, description="opacity adjustment")
        _check(native_state(child, timeout=timeout)["frame"] == before_frame, "Opacity adjustment moved the window")
        steps.append({"requested": value, "confirmed": context["opacity"], **actual})
    return {"initial": initial, "steps": steps, "camera_and_window_unchanged": True}


def check_roi_boxes(child, *, expected_rois=None, expected_index=None, timeout=30, description="ROI boxes"):
    """Compare rendered corners with an independent image-to-screen calculation."""
    def inspect():
        value = child.evaluate_js("""(() => {
          const view=nd2Translucent,svg=document.getElementById('roi-overlay');
          if(!svg) return null;
          const style=getComputedStyle(svg),rect=svg.getBoundingClientRect();
          return {rois:view.state.rois,index:view.state.roiIndex,
            layer:{width:rect.width,height:rect.height,opacity:Number(style.opacity),
              pointerEvents:style.pointerEvents,outsideImage:!document.getElementById('translucent-stage').contains(svg)},
            boxes:[...svg.querySelectorAll('polygon.roi-box')].map(box=>{
              const s=getComputedStyle(box);
              return {index:Number(box.dataset.roiIndex),selected:box.dataset.selected==='true',
                points:[...box.points].map(p=>[p.x,p.y]),stroke:s.stroke,width:Number.parseFloat(s.strokeWidth),
                pointerEvents:s.pointerEvents,vectorEffect:s.vectorEffect,display:s.display};
            }).sort((a,b)=>a.index-b.index)};
        })()""")
        if not value or not value["rois"] or len(value["boxes"]) != len(value["rois"]):
            return None
        view = camera(child)
        theta = math.radians(view["rotation"])
        cosine, sine = math.cos(theta), math.sin(theta)
        width, height = view["size"]
        maximum_error = 0
        for index, (roi, box) in enumerate(zip(value["rois"], value["boxes"])):
            if box["index"] != index or len(box["points"]) != 4:
                return None
            x, y, w, h = (roi[key] for key in ("x", "y", "w", "h"))
            expected = []
            for px, py in ((x, y), (x + w, y), (x + w, y + h), (x, y + h)):
                dx, dy = (px - view["center"][0]) * view["zoom"], (py - view["center"][1]) * view["zoom"]
                sx = width / 2 + dx * cosine - dy * sine
                sy = height / 2 + dx * sine + dy * cosine
                expected.append([width - sx if view["flip"] else sx, sy])
            error = max(abs(a - b) for actual, wanted in zip(box["points"], expected)
                        for a, b in zip(actual, wanted))
            maximum_error = max(maximum_error, error)
        # An update-viewport paint can trail the camera by one animation frame.
        if maximum_error > .15:
            return None
        return {**value, "camera": view, "maximum_corner_error_css_px": maximum_error}

    value = wait_for(inspect, timeout=timeout, description=description + " aligned corners")
    rois, selected, layer = value["rois"], value["index"], value["layer"]
    if expected_rois is not None:
        _check(rois == expected_rois, f"{description}: canonical ROI geometry changed")
    if expected_index is not None:
        _check(selected == expected_index, f"{description}: wrong selected ROI")
    _check(layer["outsideImage"] and layer["opacity"] == 1 and layer["pointerEvents"] == "none",
           f"{description}: ROI layer fades with the image or captures pointer input")
    _check([layer["width"], layer["height"]] == value["camera"]["size"],
           f"{description}: ROI layer does not fill the viewer")
    for index, box in enumerate(value["boxes"]):
        _check(box["selected"] == (index == selected), f"{description}: selection highlight is stale")
        _check(box["stroke"] == ("rgb(255, 217, 102)" if index == selected else "rgb(160, 166, 174)"),
               f"{description}: ROI colors differ from the main viewer")
        _check(math.isclose(box["width"], 1.5) and box["vectorEffect"] == "non-scaling-stroke",
               f"{description}: ROI stroke does not stay at 1.5 screen pixels")
        _check(box["pointerEvents"] == "none", f"{description}: ROI box captures pointer input")
        _check(box["display"] != "none", f"{description}: ROI box is hidden")
    return value


def snapshot_roi_boxes(child, path, geometry, *, timeout=30):
    """Confirm visible golden strokes in this overlay's own native snapshot."""
    from PIL import Image

    path = save_cocoa_snapshot(child, path, timeout=timeout)
    selected = next(box for box in geometry["boxes"] if box["selected"])
    with Image.open(path) as image:
        pixels = image.convert("RGBA")
        sx, sy = image.width / geometry["camera"]["size"][0], image.height / geometry["camera"]["size"][1]
        matched, samples = 0, 0
        points = selected["points"]
        for start, end in zip(points, points[1:] + points[:1]):
            for fraction in (.25, .5, .75):
                x = round((start[0] + fraction * (end[0] - start[0])) * sx)
                y = round((start[1] + fraction * (end[1] - start[1])) * sy)
                _check(3 <= x < image.width - 3 and 3 <= y < image.height - 3,
                       "ROI snapshot sample is clipped")
                nearby = pixels.crop((x - 3, y - 3, x + 4, y + 4))
                matched += any(r > 200 and g > 150 and b < 170 and a > 230
                               for r, g, b, a in nearby.getdata())
                samples += 1
    _check(matched >= 10, f"Selected ROI stroke is not visibly opaque: {matched}/{samples} edge samples")
    return {"path": str(path), "visible_selected_edge_samples": matched, "edge_samples": samples}


def exercise_roi_boxes(child, *, timeout=30, screenshot_path=None):
    """Add temporary diagnostic boxes only to the newly owned overlay."""
    before = camera(child)
    size = child.evaluate_js("(() => {const p=nd2Translucent.viewer.world.getItemAt(0).getContentSize();return [p.x,p.y];})()")
    width, height = size
    rois = [
        {"x": width * .30, "y": height * .30, "w": width * .12, "h": height * .14,
         "index": 7, "label": "QA first"},
        {"x": width * .52, "y": height * .44, "w": width * .14, "h": height * .12,
         "index": 19, "label": "QA second"},
    ]
    payload = {"ok": True, "name": "private diagnostic ROI boxes", "rois": rois}
    _check(child.evaluate_js("nd2Translucent.setRois(" + json.dumps(payload) + ",{pan:false,index:0})"),
           "Diagnostic ROI boxes were not accepted")
    checks = {"initial": check_roi_boxes(child, expected_rois=rois, expected_index=0, timeout=timeout)}
    check_camera(camera(child), before, description="ROI boxes initial paint")
    child.evaluate_js("document.getElementById('roi-next').click(); true")
    checks["next"] = check_roi_boxes(child, expected_rois=rois, expected_index=1,
                                    timeout=timeout, description="next ROI")
    check_camera(camera(child), before, center=False, description="ROI box jump scale")
    center = [rois[1]["x"] + rois[1]["w"] / 2, rois[1]["y"] + rois[1]["h"] / 2]
    _check(max(abs(a - b) for a, b in zip(camera(child)["center"], center)) < .01,
           "Selected ROI box is not centered after navigation")
    child.evaluate_js("nd2Translucent.viewer.viewport.setRotation(37,true); "
                      "nd2Translucent.viewer.viewport.setFlip(false); nd2Translucent.viewer.forceRedraw(); true")
    checks["rotated"] = check_roi_boxes(child, expected_rois=rois, expected_index=1,
                                       timeout=timeout, description="rotated ROI")
    child.evaluate_js("nd2Translucent.viewer.viewport.setFlip(true); nd2Translucent.viewer.forceRedraw(); true")
    checks["flipped"] = check_roi_boxes(child, expected_rois=rois, expected_index=1,
                                       timeout=timeout, description="flipped ROI")
    if screenshot_path is not None and sys.platform == "darwin":
        time.sleep(.2)
        checks["snapshot"] = snapshot_roi_boxes(
            child, Path(screenshot_path).with_name("translucent-roi-boxes.png"), checks["flipped"], timeout=timeout)
    child.evaluate_js("""(() => {
      const v=nd2Translucent.viewer,p=v.viewport;
      p.zoomTo(p.getZoom(true)*1.2,null,true);
      p.panBy(new OpenSeadragon.Point(.025,.018),true);v.forceRedraw();return true;
    })()""")
    checks["pan_zoom"] = check_roi_boxes(child, expected_rois=rois, expected_index=1,
                                        timeout=timeout, description="pan and zoom ROI")
    _check(bridge_result(child, "pywebview.api.resize_window(680,492)", timeout=timeout)["ok"],
           "ROI native resize failed")
    wait_for(lambda: child.evaluate_js("innerWidth===680 && innerHeight===492"),
             timeout=timeout, description="ROI resized window")
    checks["resized"] = check_roi_boxes(child, expected_rois=rois, expected_index=1,
                                       timeout=timeout, description="resized ROI")
    check_camera(checks["resized"]["camera"], checks["pan_zoom"]["camera"], description="ROI resize scale")
    _check(bridge_result(child, f"pywebview.api.resize_window({before['size'][0]},{before['size'][1]})",
                         timeout=timeout)["ok"], "Could not restore ROI check window size")
    wait_for(lambda: child.evaluate_js(f"innerWidth==={before['size'][0]} && innerHeight==={before['size'][1]}"),
             timeout=timeout, description="restore ROI window size")
    child.evaluate_js("""(() => {
      const p=nd2Translucent.viewer.viewport,saved=""" + json.dumps(before) + """;
      p.setRotation(saved.rotation,true);p.setFlip(saved.flip);
      p.zoomTo(p.imageToViewportZoom(saved.zoom),null,true);
      p.panTo(p.imageToViewportCoordinates(...saved.center),true);nd2Translucent.viewer.forceRedraw();return true;
    })()""")
    checks["restored"] = check_roi_boxes(child, expected_rois=rois, expected_index=1,
                                        timeout=timeout, description="restored ROI")
    check_camera(camera(child), before, description="ROI check camera restoration")
    return {"ok": True, "scope": "temporary overlay-only diagnostic ROIs", "source_geometry_unchanged": True,
            "checks": checks}


def sample_canvas_patch(window, image_x, image_y, *, is_pane=False):
    """Sample a 5x5 image-centered patch from the OSD 2D canvas (pre-opacity)."""
    viewer_expr = "state.viewer" if is_pane else "window.nd2Translucent && window.nd2Translucent.viewer"
    script = f"""(() => {{
      const viewer = {viewer_expr};
      if (!viewer || !viewer.viewport || !viewer.drawer) return {{ error: "viewer not ready" }};
      const vp = viewer.viewport;
      const size = vp.getContainerSize();
      const canvas = viewer.drawer.canvas;
      const ctx = viewer.drawer.context || (canvas && canvas.getContext("2d"));
      if (!canvas || !ctx || !(size.x > 0) || !(size.y > 0) || !(canvas.width > 0) || !(canvas.height > 0)) {{
        return {{ error: "canvas not ready" }};
      }}
      const sx = canvas.width / size.x;
      const sy = canvas.height / size.y;
      const coords = [];
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      for (let dy = -2; dy <= 2; dy++) {{
        for (let dx = -2; dx <= 2; dx++) {{
          const pt = Nd2ViewerGeometry.imageToViewerElementPoint(
            vp, {{ x: {float(image_x)!r} + dx + 0.5, y: {float(image_y)!r} + dy + 0.5 }}, OpenSeadragon
          );
          const bx = Math.floor(pt.x * sx);
          const by = Math.floor(pt.y * sy);
          if (!Number.isFinite(bx) || !Number.isFinite(by) ||
              bx < 0 || bx >= canvas.width || by < 0 || by >= canvas.height) {{
            return {{
              error: "backing coordinate out of bounds",
              coord: [bx, by],
              canvas: [canvas.width, canvas.height],
            }};
          }}
          coords.push([bx, by]);
          if (bx < minX) minX = bx;
          if (by < minY) minY = by;
          if (bx > maxX) maxX = bx;
          if (by > maxY) maxY = by;
        }}
      }}
      const bw = maxX - minX + 1;
      const bh = maxY - minY + 1;
      const img = ctx.getImageData(minX, minY, bw, bh).data;
      let sumR = 0, sumG = 0, sumB = 0, minAlpha = 255;
      for (const [bx, by] of coords) {{
        const off = ((by - minY) * bw + (bx - minX)) * 4;
        sumR += img[off];
        sumG += img[off + 1];
        sumB += img[off + 2];
        if (img[off + 3] < minAlpha) minAlpha = img[off + 3];
      }}
      const n = coords.length;
      const meanRgb = [sumR / n, sumG / n, sumB / n];
      return {{
        mean_rgb: meanRgb,
        mean_intensity: (meanRgb[0] + meanRgb[1] + meanRgb[2]) / 3,
        min_alpha: minAlpha,
        samples: n,
      }};
    }})()"""
    result = None
    for attempt in range(3):
        result = pane_eval(window, script) if is_pane else window.evaluate_js(script)
        if result is not None:
            break
        if attempt < 2:
            time.sleep(.05)
    if not isinstance(result, dict) or "error" in result:
        raise AssertionError(f"sample_canvas_patch failed ({'pane' if is_pane else 'overlay'}): {result}")
    return result


def find_signal_patch(window, *, is_pane=False):
    """Scan a coarse interior grid for an opaque 5x5 patch with mean_intensity in [15, 160]."""
    viewer_expr = "state.viewer" if is_pane else "window.nd2Translucent && window.nd2Translucent.viewer"
    script = f"""(() => {{
      const viewer = {viewer_expr};
      if (!viewer || !viewer.viewport || !viewer.drawer) return {{ error: "viewer not ready" }};
      const item = viewer.world && viewer.world.getItemAt(0);
      if (!item) return {{ error: "no tiled image" }};
      const content = item.getContentSize();
      const vp = viewer.viewport;
      const size = vp.getContainerSize();
      const canvas = viewer.drawer.canvas;
      const ctx = viewer.drawer.context || (canvas && canvas.getContext("2d"));
      if (!canvas || !ctx || !(size.x > 0) || !(size.y > 0) || !(canvas.width > 0) || !(canvas.height > 0)) {{
        return {{ error: "canvas not ready" }};
      }}
      const sx = canvas.width / size.x;
      const sy = canvas.height / size.y;
      const steps = 13;
      let best = null;
      for (let gy = 0; gy < steps; gy++) {{
        const ey = size.y * (0.12 + 0.76 * gy / (steps - 1));
        for (let gx = 0; gx < steps; gx++) {{
          const ex = size.x * (0.12 + 0.76 * gx / (steps - 1));
          const imgPt = Nd2ViewerGeometry.viewerElementToImagePoint(vp, {{ x: ex, y: ey }}, OpenSeadragon);
          const ix = Math.floor(imgPt.x);
          const iy = Math.floor(imgPt.y);
          if (!Number.isFinite(ix) || !Number.isFinite(iy) ||
              ix < 3 || iy < 3 || ix >= content.x - 3 || iy >= content.y - 3) continue;
          const coords = [];
          let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
          let inBounds = true;
          for (let dy = -2; dy <= 2 && inBounds; dy++) {{
            for (let dx = -2; dx <= 2; dx++) {{
              const pt = Nd2ViewerGeometry.imageToViewerElementPoint(
                vp, {{ x: ix + dx + 0.5, y: iy + dy + 0.5 }}, OpenSeadragon
              );
              const bx = Math.floor(pt.x * sx);
              const by = Math.floor(pt.y * sy);
              if (!Number.isFinite(bx) || !Number.isFinite(by) ||
                  bx < 0 || bx >= canvas.width || by < 0 || by >= canvas.height) {{
                inBounds = false;
                break;
              }}
              coords.push([bx, by]);
              if (bx < minX) minX = bx;
              if (by < minY) minY = by;
              if (bx > maxX) maxX = bx;
              if (by > maxY) maxY = by;
            }}
          }}
          if (!inBounds) continue;
          const bw = maxX - minX + 1;
          const bh = maxY - minY + 1;
          const data = ctx.getImageData(minX, minY, bw, bh).data;
          let sumR = 0, sumG = 0, sumB = 0, minA = 255;
          for (const [bx, by] of coords) {{
            const off = ((by - minY) * bw + (bx - minX)) * 4;
            sumR += data[off];
            sumG += data[off + 1];
            sumB += data[off + 2];
            if (data[off + 3] < minA) minA = data[off + 3];
          }}
          if (minA !== 255) continue;
          const mean = (sumR + sumG + sumB) / (3 * coords.length);
          if (mean < 15 || mean > 160) continue;
          const score = Math.abs(mean - 60);
          if (!best || score < best.score) {{
            best = {{ x: ix, y: iy, mean, score }};
          }}
        }}
      }}
      return best ? {{ x: best.x, y: best.y, mean: best.mean }} : {{ error: "no interior patch in [15, 160]" }};
    }})()"""
    result = pane_eval(window, script) if is_pane else window.evaluate_js(script)
    if not isinstance(result, dict) or "error" in result:
        raise AssertionError(f"find_signal_patch failed ({'pane' if is_pane else 'overlay'}): {result}")
    return (float(result["x"]), float(result["y"]))


def _overlay_display_query(child):
    return child.evaluate_js("""(() => {
      const view = window.nd2Translucent;
      if (typeof view?.state?.query === "string") return view.state.query;
      const url = view?.viewer?.world?.getItemAt?.(0)?.source?.getTileUrl?.(0, 0, 0) || "";
      const idx = url.indexOf("?");
      return idx >= 0 ? url.slice(idx + 1) : "";
    })()""")


def _dispatch_lut_hi_pointer(parent, ch_index, event_type, target_value, pointer_id=71):
    return pane_eval(
        parent,
        f"""(() => {{
          const canvas = document.querySelectorAll('.lut-canvas')[{int(ch_index)}];
          if (!canvas) return {{ error: 'missing lut canvas' }};
          const r = canvas.getBoundingClientRect();
          const vmin = Number(canvas.getAttribute('data-axis-min'));
          const vmax = Number(canvas.getAttribute('data-axis-max'));
          const x0 = 4, x1 = r.width - 4;
          const frac = Math.max(0, Math.min(1, ({float(target_value)!r} - vmin) / (vmax - vmin)));
          const cx = r.left + x0 + frac * (x1 - x0);
          const cy = r.top + 8;
          canvas.dispatchEvent(new PointerEvent({json.dumps(event_type)}, {{
            pointerId: {int(pointer_id)}, pointerType: 'mouse', isPrimary: true,
            button: 0, buttons: {0 if event_type in ('pointerup', 'pointercancel') else 1},
            clientX: cx, clientY: cy, bubbles: true, cancelable: true,
          }}));
          return {{ val: {float(target_value)!r}, cx, cy, vmin, vmax, lut: state.luts[{int(ch_index)}] }};
        }})()""",
    )


def _pick_drag_channel(parent, sample_rgb):
    info = pane_eval(
        parent,
        """(() => {
          const canvases = [...document.querySelectorAll('.lut-canvas')];
          const discrete = /^(?:u?int\\d+|bool|[<>=|]?[iu]\\d+)$/.test(String(state.info?.dtype || ''));
          const enabled = Array.isArray(state.channels) && state.channels.length
            ? [...state.channels] : [0];
          return {
            discrete,
            enabled,
            channels: state.info.channels.map((ch, i) => {
              const c = canvases[i];
              const cur = state.luts[i] || { lo: ch.window.start, hi: ch.window.end, gamma: 1 };
              return {
                index: i,
                color: String(ch.color || 'FFFFFF').replace(/^#/, '').toUpperCase(),
                cur: { lo: Number(cur.lo), hi: Number(cur.hi), gamma: Number(cur.gamma ?? 1) },
                hasOverride: Boolean(state.luts[i]),
                vmin: c ? Number(c.getAttribute('data-axis-min')) : NaN,
                vmax: c ? Number(c.getAttribute('data-axis-max')) : NaN,
              };
            }),
          };
        })()""",
    )
    best = None
    for idx in info["enabled"]:
        ch = info["channels"][idx]
        if not (math.isfinite(ch["vmin"]) and math.isfinite(ch["vmax"]) and ch["vmax"] > ch["vmin"]):
            continue
        if not (ch["cur"]["hi"] > ch["cur"]["lo"] and ch["vmin"] <= ch["cur"]["hi"] <= ch["vmax"]):
            continue
        hex_col = ch["color"] if len(ch["color"]) == 6 else "FFFFFF"
        rgb = (int(hex_col[0:2], 16), int(hex_col[2:4], 16), int(hex_col[4:6], 16))
        dot = sum(a * b for a, b in zip(rgb, sample_rgb))
        key = (dot, ch["cur"]["hi"] - ch["cur"]["lo"], -idx)
        if best is None or key > best[0]:
            best = (key, ch)
    _check(best is not None, f"No draggable enabled LUT channel found: {info}")
    return best[1], bool(info["discrete"])


def exercise_live_lut_drag(parent, child, *, timeout=30, calibration_patch=None, locked=True):
    """Drive a real continuous white-point handle drag and verify live overlay updates."""
    pane_eval(parent, "if (state.windows.channels.isHidden()) state.windows.channels.open(); true")
    wait_for(
        lambda: pane_eval(
            parent,
            """(() => {
              if (!state.info || !state.viewer || !state.viewer.isOpen()) return false;
              if (state.lutAutoPending.some(Boolean)) return false;
              const cs = [...document.querySelectorAll('.lut-canvas')];
              if (cs.length !== state.info.channels.length || !cs.length) return false;
              return cs.every(c => {
                const r = c.getBoundingClientRect();
                const a = Number(c.getAttribute('data-axis-min'));
                const b = Number(c.getAttribute('data-axis-max'));
                return r.width >= 150 && r.height >= 60 && Number.isFinite(a) && Number.isFinite(b) && b > a;
              });
            })()""",
        ),
        timeout=timeout,
        description="channels panel and LUT axis readiness",
    )
    wait_for(lambda: pane_eval(parent, "state.viewer.world.getItemAt(0)?.getFullyLoaded()"),
             timeout=timeout, description="main tiles before live LUT drag")
    wait_for(lambda: child.evaluate_js("nd2Translucent.viewer.world.getItemAt(0)?.getFullyLoaded()"),
             timeout=timeout, description="overlay tiles before live LUT drag")

    initial_luts = pane_eval(parent, "state.luts.map(l => l ? {lo: l.lo, hi: l.hi, gamma: l.gamma} : null)")
    restore_overlay_query = _overlay_display_query(child)
    drag_start_query = restore_overlay_query
    before_camera = camera(child)
    before_frame = native_state(child, timeout=timeout)["frame"]
    before_opacity = bridge_result(child, "pywebview.api.translucent_context()", timeout=timeout)["opacity"]
    before_locked = bool(child.evaluate_js("Boolean(nd2Translucent.state.locked)"))
    before_rois = child.evaluate_js("({rois: nd2Translucent.state.rois, index: nd2Translucent.state.roiIndex})")
    _check(before_locked == bool(locked), f"Live LUT check expected locked={locked}, got {before_locked}")

    if calibration_patch is not None:
        px, py = float(calibration_patch[0]), float(calibration_patch[1])
        enabled = pane_eval(parent, "[...state.channels]") or [0]
        ch_idx = int(enabled[0])
        pane_eval(parent, f"state.lutWidgets[{ch_idx}].setLut({{lo: 0, hi: 255, gamma: 1}}); true")
        wait_for(
            lambda: (
                (m := sample_canvas_patch(parent, px, py, is_pane=True))["min_alpha"] == 255
                and abs(m["mean_intensity"] - 64.0) <= 8.0
                and (o := sample_canvas_patch(child, px, py, is_pane=False))["min_alpha"] == 255
                and abs(o["mean_intensity"] - 64.0) <= 8.0
            ),
            timeout=min(timeout, 10),
            description="calibration baseline 64 before live LUT drag",
        )
        drag_start_query = _overlay_display_query(child)
        main_init = sample_canvas_patch(parent, px, py, is_pane=True)
        overlay_init = sample_canvas_patch(child, px, py, is_pane=False)
        start_hi = 255.0
        hold_targets = [(192.0, 85.0), (128.0, 127.0)]
        release_target = (80.0, 204.0)
    else:
        px, py = find_signal_patch(child, is_pane=False)
        try:
            main_init = sample_canvas_patch(parent, px, py, is_pane=True)
        except AssertionError:
            px, py = find_signal_patch(parent, is_pane=True)
            main_init = sample_canvas_patch(parent, px, py, is_pane=True)
        overlay_init = sample_canvas_patch(child, px, py, is_pane=False)
        _check(main_init["min_alpha"] == 255 and overlay_init["min_alpha"] == 255,
               f"Initial live-LUT patch is not opaque: main={main_init}, overlay={overlay_init}")
        ch, discrete = _pick_drag_channel(parent, overlay_init["mean_rgb"])
        ch_idx = ch["index"]
        # Align widget internal handle position if state.luts was mutated directly before launch.
        if ch["hasOverride"]:
            pane_eval(
                parent,
                f"state.lutWidgets[{ch_idx}].setLut({json.dumps(ch['cur'])}); true",
            )
        lo, hi = ch["cur"]["lo"], ch["cur"]["hi"]
        span = hi - lo
        min_step = 1.0 if discrete else max(span * 1e-4, 1e-9)
        hi_1 = max(lo + 3 * min_step, lo + span * 0.72)
        hi_2 = max(lo + 2 * min_step, lo + span * 0.45)
        hi_3 = max(lo + min_step, lo + span * 0.25)
        if not (hi > hi_1 > hi_2 > hi_3 > lo):
            hi_1 = lo + span * 0.75
            hi_2 = lo + span * 0.50
            hi_3 = lo + span * 0.25
        start_hi = hi
        hold_targets = [(hi_1, None), (hi_2, None)]
        release_target = (hi_3, None)

    steps = {"initial": {"main": main_init, "overlay": overlay_init, "overlay_query": drag_start_query}}
    pre_release_queries = []
    failed = False
    try:
        _dispatch_lut_hi_pointer(parent, ch_idx, "pointerdown", start_hi)
        try:
            for step_idx, (hi_val, expected_intensity) in enumerate(hold_targets, start=1):
                _dispatch_lut_hi_pointer(parent, ch_idx, "pointermove", hi_val)
                t0 = time.monotonic()
                deadline = t0 + 1.0
                main_hit_ms = overlay_hit_ms = None
                main_sample = overlay_sample = None
                ov_query = None
                seen_queries = {drag_start_query, *pre_release_queries}
                while True:
                    now_ms = round((time.monotonic() - t0) * 1000.0, 1)
                    main_sample = sample_canvas_patch(parent, px, py, is_pane=True)
                    overlay_sample = sample_canvas_patch(child, px, py, is_pane=False)
                    ov_query = _overlay_display_query(child)
                    if expected_intensity is not None:
                        if main_hit_ms is None and main_sample["min_alpha"] == 255 and abs(main_sample["mean_intensity"] - expected_intensity) <= 8.0:
                            main_hit_ms = now_ms
                        if overlay_hit_ms is None and overlay_sample["min_alpha"] == 255 and abs(overlay_sample["mean_intensity"] - expected_intensity) <= 8.0:
                            overlay_hit_ms = now_ms
                        if main_hit_ms is not None and overlay_hit_ms is not None:
                            break
                    else:
                        min_delta = 10.0 if step_idx >= 2 else 1.0
                        if main_hit_ms is None and main_sample["min_alpha"] == 255 and main_sample["mean_intensity"] >= main_init["mean_intensity"] + min_delta:
                            main_hit_ms = now_ms
                        if (
                            overlay_hit_ms is None
                            and ov_query not in seen_queries
                            and overlay_sample["min_alpha"] == 255
                            and overlay_sample["mean_intensity"] >= overlay_init["mean_intensity"] + min_delta
                        ):
                            overlay_hit_ms = now_ms
                        if main_hit_ms is not None and overlay_hit_ms is not None:
                            break
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(.05)
                if ov_query and ov_query != drag_start_query and ov_query not in pre_release_queries:
                    pre_release_queries.append(ov_query)
                steps[f"hold_{step_idx}"] = {
                    "hi": hi_val,
                    "expected_intensity": expected_intensity,
                    "main_elapsed_ms": main_hit_ms,
                    "overlay_elapsed_ms": overlay_hit_ms,
                    "main": main_sample,
                    "overlay": overlay_sample,
                    "overlay_query": ov_query,
                }
            _dispatch_lut_hi_pointer(parent, ch_idx, "pointermove", release_target[0])
        finally:
            _dispatch_lut_hi_pointer(parent, ch_idx, "pointerup", release_target[0])

        t0 = time.monotonic()
        deadline = t0 + min(timeout, 5.0)
        main_hit_ms = overlay_hit_ms = None
        main_rel = overlay_rel = None
        rel_query = None
        prev_overlay = steps["hold_2"]["overlay"]["mean_intensity"]
        prev_main = steps["hold_2"]["main"]["mean_intensity"]
        while True:
            now_ms = round((time.monotonic() - t0) * 1000.0, 1)
            main_rel = sample_canvas_patch(parent, px, py, is_pane=True)
            overlay_rel = sample_canvas_patch(child, px, py, is_pane=False)
            rel_query = _overlay_display_query(child)
            if release_target[1] is not None:
                if main_hit_ms is None and main_rel["min_alpha"] == 255 and abs(main_rel["mean_intensity"] - release_target[1]) <= 8.0:
                    main_hit_ms = now_ms
                if overlay_hit_ms is None and overlay_rel["min_alpha"] == 255 and abs(overlay_rel["mean_intensity"] - release_target[1]) <= 8.0:
                    overlay_hit_ms = now_ms
                if main_hit_ms is not None and overlay_hit_ms is not None:
                    break
            else:
                if main_hit_ms is None and main_rel["min_alpha"] == 255 and main_rel["mean_intensity"] >= prev_main - 2.0:
                    main_hit_ms = now_ms
                if (
                    overlay_hit_ms is None
                    and rel_query not in {drag_start_query, *pre_release_queries}
                    and overlay_rel["min_alpha"] == 255
                    and overlay_rel["mean_intensity"] >= prev_overlay - 2.0
                ):
                    overlay_hit_ms = now_ms
                if main_hit_ms is not None and overlay_hit_ms is not None:
                    break
            if time.monotonic() >= deadline:
                break
            time.sleep(.05)
        steps["release"] = {
            "hi": release_target[0],
            "expected_intensity": release_target[1],
            "main_elapsed_ms": main_hit_ms,
            "overlay_elapsed_ms": overlay_hit_ms,
            "main": main_rel,
            "overlay": overlay_rel,
            "overlay_query": rel_query,
        }

        if calibration_patch is not None:
            for key, target in (("hold_1", 85.0), ("hold_2", 127.0), ("release", 204.0)):
                st = steps[key]
                _check(st["main"]["min_alpha"] == 255 and abs(st["main"]["mean_intensity"] - target) <= 8.0,
                       f"Live LUT calibration main {key} missed {target}±8: {st['main']}")
                _check(st["overlay"]["min_alpha"] == 255 and abs(st["overlay"]["mean_intensity"] - target) <= 8.0,
                       f"Live LUT calibration overlay {key} missed {target}±8: {st['overlay']}")
                if key != "release":
                    _check(st["main_elapsed_ms"] is not None and st["overlay_elapsed_ms"] is not None,
                           f"Live LUT calibration {key} did not paint before pointerup: {st}")
        else:
            _check(len(pre_release_queries) >= 2,
                   f"Expected >= 2 distinct pre-release overlay query updates, got {pre_release_queries}")
            for key in ("hold_1", "hold_2", "release"):
                _check(steps[key]["main"]["min_alpha"] == 255 and steps[key]["overlay"]["min_alpha"] == 255,
                       f"Live LUT drag {key} canvas was not opaque: {steps[key]}")
            ov0 = overlay_init["mean_intensity"]
            ov1 = steps["hold_1"]["overlay"]["mean_intensity"]
            ov2 = steps["hold_2"]["overlay"]["mean_intensity"]
            ov3 = steps["release"]["overlay"]["mean_intensity"]
            m0 = main_init["mean_intensity"]
            m2 = steps["hold_2"]["main"]["mean_intensity"]
            _check(ov1 >= ov0 - 1.0 and ov2 >= ov1 - 2.0 and ov2 - ov0 >= 10.0,
                   f"Overlay did not brighten monotonically by >= +10 before release: {[ov0, ov1, ov2, ov3]}")
            _check(ov3 >= ov2 - 2.0 and m2 - m0 >= 10.0,
                   f"Main/overlay brightening failed: main={[m0, m2]} overlay={[ov0, ov1, ov2, ov3]}")

        after_camera = camera(child)
        check_camera(after_camera, before_camera, description=f"live LUT drag (locked={locked})")
        _check(after_camera["rotation"] == before_camera["rotation"] and after_camera["flip"] == before_camera["flip"],
               f"Live LUT drag changed orientation (locked={locked})")
        _check(native_state(child, timeout=timeout)["frame"] == before_frame,
               f"Live LUT drag moved native overlay frame (locked={locked})")
        _check(bool(child.evaluate_js("Boolean(nd2Translucent.state.locked)")) == bool(locked),
               f"Live LUT drag changed lock state (locked={locked})")
        _check(math.isclose(bridge_result(child, "pywebview.api.translucent_context()", timeout=timeout)["opacity"], before_opacity),
               f"Live LUT drag changed overlay opacity (locked={locked})")
        if before_rois["rois"]:
            check_roi_boxes(child, expected_rois=before_rois["rois"], expected_index=before_rois["index"],
                            timeout=timeout, description=f"live LUT drag ROI (locked={locked})")
        return {
            "ok": True,
            "locked": bool(locked),
            "channel": ch_idx,
            "patch": [px, py],
            "calibration": calibration_patch is not None,
            "pre_release_overlay_queries": pre_release_queries,
            "steps": steps,
            "camera_and_window_unchanged": True,
        }
    except Exception:
        failed = True
        raise
    finally:
        pane_eval(
            parent,
            f"""(() => {{
              const saved = {json.dumps(initial_luts)};
              for (let i = 0; i < state.lutWidgets.length; i++) {{
                if (saved[i]) state.lutWidgets[i].setLut(saved[i]);
                else state.lutWidgets[i].reset();
              }}
              return true;
            }})()""",
        )
        try:
            wait_for(
                lambda: _overlay_display_query(child) == restore_overlay_query
                and child.evaluate_js("Boolean(nd2Translucent.viewer.world.getItemAt(0)?.getFullyLoaded())"),
                timeout=min(timeout, 5.0),
                description="live LUT restoration",
            )
        except TimeoutError:
            if not failed:
                raise


def exercise_translucent_viewer(api, parent, *, timeout=30, screenshot_path=None, extra_checks=None):
    """Exercise one newly-created overlay and close it, returning bounded evidence."""
    result = {"ok": False, "platform": sys.platform}
    overlay, child, restore_parent = None, None, False
    try:
        _check(getattr(parent, "_js_api", None) is api, "The supplied bridge does not own this parent window")
        wait_for(lambda: pane_eval(parent, "!!(state.info && state.annReady && state.viewer.isOpen())"),
                 timeout=timeout, description="source image")
        source = pane_eval(parent, "captureTranslucentView()")
        result["source"] = source
        previous = set(api._translucent_windows)
        _check(not previous, "Smoke check requires a parent without existing overlays")
        click_shell_cloud(parent, timeout=timeout)
        overlay = wait_for(lambda: next(iter(api._translucent_windows.values()), None),
                           timeout=timeout, description="cloud button overlay")
        child = overlay._window
        result["window_id"] = child.uid
        wait_for(lambda: child.evaluate_js("!!window.nd2Translucent?.state.ready"),
                 timeout=timeout, description="overlay image")
        wait_for(lambda: overlay._configured, timeout=timeout, description="native overlay setup")
        context = bridge_result(child, "pywebview.api.translucent_context()", timeout=timeout)
        _check(context["query"] == source["query"], "Overlay changed the source channel/LUT/plane query")
        check_camera(camera(child), source, description="initial handoff")
        _check(camera(child)["rotation"] == source["rotation"] and camera(child)["flip"] == source["flip"],
               "Image orientation was not preserved")
        result["native_initial"] = native_state(child, timeout=timeout)
        _check(result["native_initial"]["topmost"] and not result["native_initial"]["has_parent"],
               "Overlay is not an independent topmost native window")
        if sys.platform == "darwin":
            _check(not result["native_initial"]["opaque"] and result["native_initial"]["background_alpha"] == 0,
                   "Native window background is opaque")
        else:
            _check(math.isclose(result["native_initial"]["opacity"], .55), "Native Windows opacity was not applied")
        styles = child.evaluate_js("""(() => {
          const button=document.getElementById('overlay-lock');
          return {background:getComputedStyle(button).backgroundColor,
            color:getComputedStyle(button).color,radius:getComputedStyle(document.getElementById('overlay')).borderRadius,
            navHidden:document.getElementById('roi-controls').hidden};
        })()""")
        _check(styles["background"] == "rgba(0, 0, 0, 0)" and styles["color"] == "rgb(255, 255, 255)",
               "Lock control has an unwanted background or non-white glyph")
        _check(styles["navHidden"] == (not bool(source.get("rois"))), "ROI navigation visibility does not match imported data")
        result["styles"] = styles
        result["opacity_control"] = exercise_opacity_controls(child, timeout=timeout, screenshot_path=screenshot_path)

        before = camera(child)
        _check(bridge_result(child, "pywebview.api.resize_window(744, 528)", timeout=timeout)["ok"], "Native resize failed")
        wait_for(lambda: child.evaluate_js("innerWidth===744 && innerHeight===528"), timeout=timeout, description="window resize")
        time.sleep(.2)
        check_camera(camera(child), before, description="resize")
        result["resize_preserves_camera"] = True
        before_native = native_state(child, timeout=timeout)
        _check(bridge_result(child, "pywebview.api.move_window(31, 19)", timeout=timeout), "Native move failed")
        wait_for(lambda: native_state(child, timeout=timeout)["frame"][:2] != before_native["frame"][:2],
                 timeout=timeout, description="native move")
        after_native = native_state(child, timeout=timeout)
        _check(abs(after_native["frame"][0] - before_native["frame"][0] - 31) < 1,
               "Native horizontal movement changed coordinate scale")
        result["native_move"] = {"before": before_native["frame"], "after": after_native["frame"]}

        before = camera(child)
        wheel(child)
        wait_for(lambda: abs(camera(child)["zoom"] - before["zoom"]) > 1e-5, timeout=timeout, description="DOM wheel zoom")
        time.sleep(.3)
        after_wheel = camera(child)
        drag_image(child)
        wait_for(lambda: max(abs(a - b) for a, b in zip(camera(child)["center"], after_wheel["center"])) > .1,
                 timeout=timeout, description="DOM pointer image pan")
        time.sleep(.2)
        result["wheel_and_pointer_pan"] = {"before": before, "after_wheel": after_wheel, "after_pan": camera(child)}
        if screenshot_path is not None and sys.platform == "darwin":
            # Wait for actual JPEG tiles, not just the OSD open event.
            wait_for(lambda: child.evaluate_js("nd2Translucent.viewer.world.getItemAt(0).getFullyLoaded()"),
                     timeout=timeout, description="overlay tiles")
            time.sleep(.2)
            result["snapshot"] = snapshot_cocoa(child, screenshot_path, timeout=timeout)
        source_annotations = pane_eval(parent, "JSON.stringify(state.annotations)")
        result["roi_boxes"] = exercise_roi_boxes(child, timeout=timeout, screenshot_path=screenshot_path)
        _check(pane_eval(parent, "JSON.stringify(state.annotations)") == source_annotations,
               "ROI box diagnostic changed source annotations")
        result["live_lut_unlocked"] = exercise_live_lut_drag(parent, child, timeout=timeout, locked=False)
        if extra_checks is not None:
            result["extra"] = extra_checks(overlay, child)

        child.evaluate_js("document.getElementById('overlay-lock').click(); true")
        wait_for(lambda: child.evaluate_js("nd2Translucent.state.locked && !nd2Translucent.state.pending"),
                 timeout=timeout, description="overlay lock")
        _check(child.evaluate_js("document.getElementById('overlay-opacity').disabled"), "Lock did not disable the opacity slider")
        rejected_opacity = bridge_result(child, "pywebview.api.set_translucent_opacity(.3)", timeout=timeout)
        _check(not rejected_opacity["ok"] and math.isclose(rejected_opacity["opacity"], .55),
               "Locked native bridge accepted an opacity change")
        _check(math.isclose(bridge_result(child, "pywebview.api.translucent_context()", timeout=timeout)["opacity"], .55),
               "Locked opacity changed its stored value")
        if sys.platform == "win32":
            _check(math.isclose(native_state(child, timeout=timeout)["opacity"], .55), "Locked native opacity changed")
        result["locked_opacity_blocked"] = True
        before, before_native = camera(child), native_state(child, timeout=timeout)
        wheel(child)
        drag_image(child)
        _check(bridge_result(child, "pywebview.api.move_window(55, 44)", timeout=timeout) is False, "Locked window accepted movement")
        _check(bridge_result(child, "pywebview.api.resize_window(900, 700)", timeout=timeout)["ok"] is False,
               "Locked window accepted resize")
        time.sleep(.3)
        check_camera(camera(child), before, description="locked pan/zoom")
        _check(native_state(child, timeout=timeout)["frame"] == before_native["frame"], "Locked native frame moved")
        result["lock_blocks_image_and_window"] = True
        result["live_lut_locked"] = exercise_live_lut_drag(parent, child, timeout=timeout, locked=True)
        parent.minimize()
        restore_parent = True
        wait_for(lambda: native_state(parent, timeout=timeout)["minimized"], timeout=timeout, description="own parent minimize")
        visible = native_state(child, timeout=timeout)
        _check(visible["visible"] and visible["topmost"] and not visible["minimized"],
               "Overlay disappeared when its source viewer was minimized")
        result["survives_parent_minimize"] = True
        # The actual X remains usable while locked and while the parent is minimized.
        child.evaluate_js("document.getElementById('overlay-close').click(); true")
        wait_for(lambda: overlay._closed and not api._translucent_windows, timeout=timeout, description="locked overlay close")
        result["locked_close"] = True
        result["ok"] = True
    except Exception:
        result["error"] = traceback.format_exc()
    finally:
        if overlay is not None and not overlay._closed:
            try:
                overlay.close_translucent()
            except Exception as exc:
                result["cleanup_error"] = str(exc)
        if restore_parent:
            parent.restore()
    return result
