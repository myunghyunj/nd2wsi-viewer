"""Check the title-bar double-click in a real window.

Starts the app on the example scan, dispatches synthetic double-clicks on the
tab strip, its buttons, and a slide's toolbar, and counts the calls that reach
the native window on both sides of the bridge. Needs a display and the app
extras. Run from the repo:

    PYTHONPATH=$PWD uv run --frozen python scripts/check_titlebar_zoom.py
"""

import json
import sys
import threading
import time
from pathlib import Path

import AppKit
import webview
from Foundation import NSOperationQueue

from nd2wsi import app as A

CALLS = []


class CountingApi(A.Api):
    def title_bar_double_click(self):
        r = super().title_bar_double_click()
        CALLS.append(r)
        return r


A.Api = CountingApi
OUT = {"cases": []}


def on_main(fn, timeout=8.0):
    done = threading.Event()
    box = {}

    def run():
        try:
            box["v"] = fn()
        except Exception as e:
            box["e"] = repr(e)
        done.set()

    NSOperationQueue.mainQueue().addOperationWithBlock_(run)
    done.wait(timeout)
    return box.get("v")


def zoomed(window):
    return int(on_main(lambda: A._native_window(window).isZoomed()))


def fullscreen(window):
    return int(
        bool(
            on_main(
                lambda: A._native_window(window).styleMask() & AppKit.NSWindowStyleMaskFullScreen
            )
        )
    )


def frame(window):
    return on_main(
        lambda: tuple(
            int(v)
            for v in (
                A._native_window(window).frame().size.width,
                A._native_window(window).frame().size.height,
            )
        )
    )


def wait_js(window, expr, timeout=120.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if window.evaluate_js(expr):
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


DISPATCH = "(() => { const f = document.querySelector('#frames iframe'); const d = f.contentDocument; const el = %s; el.dispatchEvent(new (el.ownerDocument.defaultView.MouseEvent)('dblclick', {bubbles: true, cancelable: true})); })(); 1"


def probe(api, window):
    ok = True
    try:
        wait_js(window, "!!document.getElementById('tabbar-space')")
        wait_js(
            window,
            "(() => { const f = document.querySelector('#frames iframe'); return !!(f && f.contentDocument && f.contentDocument.getElementById('toolbar')); })()",
        )
        time.sleep(3.0)
        window.evaluate_js(
            "window.__zoomCalls = 0; window.requestWindowZoom = (orig => function () { window.__zoomCalls++; return orig(); })(window.requestWindowZoom); 1"
        )

        def case(name, js, expect_calls):
            nonlocal ok
            z0 = zoomed(window)
            n0 = len(CALLS)
            j0 = window.evaluate_js("window.__zoomCalls")
            window.evaluate_js(js)
            time.sleep(1.6)
            z1 = zoomed(window)
            calls_py = len(CALLS) - n0
            calls_js = window.evaluate_js("window.__zoomCalls") - j0
            expect_z = z0 if expect_calls == 0 else 1 - z0
            passed = calls_py == expect_calls and calls_js == expect_calls and z1 == expect_z
            ok = ok and passed
            OUT["cases"].append(
                {
                    "case": name,
                    "js": calls_js,
                    "py": calls_py,
                    "expected": expect_calls,
                    "zoom": f"{z0}->{z1}",
                    "pass": passed,
                }
            )

        case("tab strip", DISPATCH % "document.getElementById('tabbar-space')", 1)
        case("chrome pad", DISPATCH % "document.getElementById('chrome-pad')", 1)
        case("tabbar padding (now inert)", DISPATCH % "document.getElementById('tabbar')", 0)
        case("tab element", DISPATCH % "document.querySelector('#tabbar .tab')", 0)
        case("newtab button", DISPATCH % "document.getElementById('newtab')", 0)
        case("toolbar", DISPATCH % "d.getElementById('toolbar')", 1)
        case("toolbar spacer", DISPATCH % "d.querySelector('#toolbar .tb-spacer')", 1)
        case("toolbar group", DISPATCH % "d.querySelector('#toolbar .tb-group')", 1)
        case("file name (now inert)", DISPATCH % "d.getElementById('file-name')", 0)
        case("tb-channels button", DISPATCH % "d.getElementById('tb-channels')", 0)
        case("badge", DISPATCH % "d.getElementById('calibration-badge')", 0)
        # a tab-close double-click: first press on the tab's ×, strip rebuilt, second press + dblclick on the bare strip
        case(
            "close-button then bare strip",
            """(() => {
            const bar = document.getElementById('tabbar'); const x = document.querySelector('#tabbar .tab .x') || document.querySelector('#tabbar .tab');
            const down = (el) => el.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true, pointerId: 1}));
            down(x); const sp = document.getElementById('tabbar-space'); down(sp);
            sp.dispatchEvent(new MouseEvent('dblclick', {bubbles: true, cancelable: true})); })(); 1""",
            0,
        )
        case(
            "two bare presses then dblclick",
            """(() => {
            const sp = document.getElementById('tabbar-space');
            const down = (el) => el.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true, pointerId: 1}));
            down(sp); down(sp); sp.dispatchEvent(new MouseEvent('dblclick', {bubbles: true, cancelable: true})); })(); 1""",
            1,
        )
        # full screen: the call happens but the window must stay full screen and unchanged
        on_main(lambda: A._native_window(window).toggleFullScreen_(None))
        time.sleep(2.5)
        fs0, f0, n0 = fullscreen(window), frame(window), len(CALLS)
        window.evaluate_js(DISPATCH % "document.getElementById('tabbar-space')")
        time.sleep(1.6)
        fs1, f1, calls = fullscreen(window), frame(window), len(CALLS) - n0
        passed = fs0 == 1 and fs1 == 1 and f0 == f1 and calls == 1
        ok = ok and passed
        OUT["fullscreen"] = {
            "was_fullscreen": fs0,
            "still_fullscreen": fs1,
            "frame_before": f0,
            "frame_after": f1,
            "py_calls": calls,
            "action": CALLS[-1] if calls else None,
            "pass": passed,
        }
        on_main(lambda: A._native_window(window).toggleFullScreen_(None))
        time.sleep(2.5)
        OUT["left_fullscreen"] = fullscreen(window) == 0
    except Exception as e:
        OUT["error"] = repr(e)
        ok = False
    finally:
        OUT["ok"] = ok
        print(json.dumps(OUT, indent=1))
        try:
            window.destroy()
        except Exception:
            pass


api, window = A.create_app_window(Path("docs/example_cell.nd2").resolve())
threading.Thread(target=lambda: (time.sleep(240), window.destroy()), daemon=True).start()
webview.start(probe, (api, window))
sys.exit(0 if OUT.get("ok") else 1)
