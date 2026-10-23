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
    return parent.evaluate_js(
        "document.querySelector('#frames iframe.active').contentWindow.eval(" +
        json.dumps(expression) + ")"
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


def snapshot_cocoa(window, path, *, timeout=30):
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
        whites = sum(1 for r, g, b, a in crop.getdata() if min(r, g, b) > 230 and a > 170)
        result = {"path": str(path), "size": [image.width, image.height], "center_rgba": middle,
                  "corner_alpha": corners, "white_lock_pixels": whites}
    _check(all(alpha < 10 for alpha in corners), f"Rounded corners are not transparent: {result}")
    _check(125 <= middle[3] <= 155, f"Image alpha is not approximately 55%: {result}")
    _check(whites > 10, f"White lock glyph missing from native snapshot: {result}")
    return result


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
        if extra_checks is not None:
            result["extra"] = extra_checks(overlay, child)

        child.evaluate_js("document.getElementById('overlay-lock').click(); true")
        wait_for(lambda: child.evaluate_js("nd2Translucent.state.locked && !nd2Translucent.state.pending"),
                 timeout=timeout, description="overlay lock")
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
