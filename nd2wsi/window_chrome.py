"""Native window presentation and shell event adapters.

Window ownership, launch policy, and server state remain in the app bridge.
These adapters receive the target window explicitly and keep optional Cocoa
imports inside macOS operations so importing the shell stays safe on Windows.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable


def native_window(window):
    """The NSWindow behind a pywebview window, or None (main thread only)."""
    import AppKit

    native = getattr(window, "native", None)
    candidate = getattr(native, "window", None) or native
    if isinstance(candidate, AppKit.NSWindow):
        return candidate
    for win in AppKit.NSApplication.sharedApplication().windows():
        if win.isVisible():
            return win
    return None


def title_bar_double_click_action() -> str:
    """What macOS does when a title bar is double-clicked: zoom, minimize, or nothing.

    Mirrors the Desktop & Dock setting. Its stored values are Maximize,
    Fill, Minimize, and None; unset means the default, which zooms.
    """
    try:
        from Foundation import NSUserDefaults

        value = NSUserDefaults.standardUserDefaults().stringForKey_(
            "AppleActionOnDoubleClick"
        )
    except Exception:
        value = None
    if value == "Minimize":
        return "minimize"
    if value == "None":
        return "none"
    return "zoom"


def inline_traffic_lights(window, *, logger: Callable[[str], None], resolve_native_window=native_window):
    """Hide the macOS title bar so the tab strip hosts the traffic lights.

    Safari-style chrome: the window keeps its close/minimize/zoom buttons,
    which float over the web content, and the shell pads its tab strip to
    make room (the ``native-chrome`` class). pywebview installs the
    WKWebView as the content view only after the first navigation, which
    can rebuild the frame, so the tweak re-runs on every ``loaded`` event
    as well as on ``shown``. Purely cosmetic — on any failure the stock
    title bar simply stays.
    """

    def apply(trigger):
        try:
            import AppKit
            from Foundation import NSOperationQueue
        except Exception:
            return

        def tweak():
            try:
                ns = resolve_native_window(window)
                if ns is None:
                    return
                # the style-mask change rebuilds the title bar, wiping any
                # appearance set before it — so the mask goes first
                ns.setStyleMask_(
                    ns.styleMask() | AppKit.NSWindowStyleMaskFullSizeContentView
                )
                ns.setTitlebarAppearsTransparent_(True)
                ns.setTitleVisibility_(AppKit.NSWindowTitleHidden)
                if hasattr(ns, "setTitlebarSeparatorStyle_"):
                    ns.setTitlebarSeparatorStyle_(
                        getattr(AppKit, "NSTitlebarSeparatorStyleNone", 1)
                    )

                frame_view = ns.contentView().superview()
                # the content view must truly span the frame: when the web
                # view was installed before the mask changed, its frame
                # still excludes the old title bar
                content = ns.contentView()
                content.setFrame_(frame_view.bounds())
                content.setAutoresizingMask_(
                    AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable
                )

                # an empty toolbar is how Chrome and Notion get their
                # traffic lights vertically centered: it makes the title
                # bar toolbar-height and the system re-seats the buttons
                if ns.toolbar() is None:
                    bar = AppKit.NSToolbar.alloc().initWithIdentifier_(
                        "nd2wsi.titlebar.spacer"
                    )
                    bar.setShowsBaselineSeparator_(False)
                    ns.setToolbar_(bar)
                if hasattr(ns, "setToolbarStyle_"):
                    ns.setToolbarStyle_(
                        getattr(AppKit, "NSWindowToolbarStyleUnifiedCompact", 4)
                    )

                # pywebview's frameless mode hides the standard buttons;
                # this app wants them, floating over the tab strip
                for which in (
                    AppKit.NSWindowCloseButton,
                    AppKit.NSWindowMiniaturizeButton,
                    AppKit.NSWindowZoomButton,
                ):
                    button = ns.standardWindowButton_(which)
                    if button is not None:
                        button.setHidden_(False)

                close_button = ns.standardWindowButton_(AppKit.NSWindowCloseButton)
                zoom_button = ns.standardWindowButton_(AppKit.NSWindowZoomButton)
                if close_button is not None and zoom_button is not None:
                    bar_h = close_button.superview().frame().size.height
                    cf, zf = close_button.frame(), zoom_button.frame()
                    logger(
                        f"lights: bar h={bar_h:.0f} close={cf.origin.x:.0f},"
                        f"{cf.origin.y:.0f} {cf.size.width:.0f}x{cf.size.height:.0f} "
                        f"zoom-right={zf.origin.x + zf.size.width:.0f}"
                    )

                # macOS 26+ draws a glass decoration layer over the title
                # bar that ignores titlebarAppearsTransparent; hide it (it
                # returns after frame rebuilds, hence the re-run)
                for sub in frame_view.subviews():
                    if "TitlebarContainer" not in str(sub.className()):
                        continue
                    for inner in sub.subviews():
                        name = str(inner.className())
                        if "Decoration" in name or "Background" in name:
                            inner.setHidden_(True)

            except Exception as e:
                logger(f"titlebar tweak failed ({trigger}): {e!r}")

        NSOperationQueue.mainQueue().addOperationWithBlock_(tweak)

    window.events.shown += lambda: apply("shown")
    window.events.loaded += lambda: apply("loaded")


def wire_file_drop(window, *, logger: Callable[[str], None]):
    """Deliver Finder drops to the page.

    pywebview 6 exposes a dropped file's real path only inside Python-side
    DOM drop handlers (the native layer records paths only while such a
    handler is registered). Page JS never sees pywebviewFullPath, so we
    register here on every page load and hand the path to the page's
    __pydrop function.
    """
    import json as _json

    from webview.dom import DOMEventHandler

    def on_drop(e):
        files = (e.get("dataTransfer") or {}).get("files") or []
        paths = [f["pywebviewFullPath"] for f in files if f.get("pywebviewFullPath")]
        logger(f"drop received {len(files)} file(s), {len(paths)} with paths")
        if paths:
            window.evaluate_js(
                f"window.__pydrop_multi ? window.__pydrop_multi({_json.dumps(paths)})"
                f" : (window.__pydrop && window.__pydrop({_json.dumps(paths[0])}))"
            )

    def attach(*_args):
        try:
            logger(f"loaded {window.get_current_url()}")
        except Exception:
            pass
        try:
            window.dom.document.events.drop += DOMEventHandler(
                on_drop, prevent_default=True
            )
        except Exception:
            pass  # not a document we control; the next load re-attaches

    window.events.loaded += attach


def install_window_menu(api, window, *, logger: Callable[[str], None]) -> None:
    """Add native shortcuts after pywebview creates the application's menu."""
    if sys.platform != "darwin" or getattr(api, "_window_menu_target", None) is not None:
        return
    try:
        import AppKit
        from Foundation import NSOperationQueue

        def install():
            try:
                if getattr(api, "_window_menu_target", None) is not None:
                    return
                # NSMenuItem does not retain its target; keep it on this Api.
                class ND2WSIWindowMenuTarget(AppKit.NSObject):
                    def newUserWindow_(self, _sender):
                        result = api.new_window("user")
                        if not result.get("ok"):
                            api._status = result.get("message", "Could not open a window.")

                    def newAgentWindow_(self, _sender):
                        result = api.new_window("agent")
                        if not result.get("ok"):
                            api._status = result.get("message", "Could not open an Agent window.")

                    def openInMetal_(self, _sender):
                        # evaluate_js must not synchronously wait on AppKit's
                        # main thread. The page resolves the active tab itself.
                        def request():
                            try:
                                window.evaluate_js("window.nd2OpenActiveInMetal?.(); true")
                            except Exception as exc:
                                api._status = f"Could not open in Metal: {exc}"
                        threading.Thread(target=request, daemon=True).start()

                    def retryInMetal_(self, _sender):
                        def request():
                            try:
                                window.evaluate_js("window.nd2OpenActiveInMetal?.(true); true")
                            except Exception as exc:
                                api._status = f"Could not retry Metal: {exc}"
                        threading.Thread(target=request, daemon=True).start()

                target = ND2WSIWindowMenuTarget.alloc().init()
                main_menu = AppKit.NSApplication.sharedApplication().mainMenu()
                if main_menu is None:
                    raise RuntimeError("native menu is not ready")
                file_item = main_menu.itemWithTitle_("File")
                if file_item is None:
                    file_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("File", None, "")
                    file_item.setSubmenu_(AppKit.NSMenu.alloc().initWithTitle_("File"))
                    main_menu.insertItem_atIndex_(file_item, min(1, main_menu.numberOfItems()))
                file_menu = file_item.submenu()
                command = AppKit.NSEventModifierFlagCommand
                shift = AppKit.NSEventModifierFlagShift
                for title, action, modifiers in (
                    ("New Window", "newUserWindow:", command),
                    ("New Agent Window", "newAgentWindow:", command | shift),
                ):
                    item = file_menu.addItemWithTitle_action_keyEquivalent_(title, action, "n")
                    item.setKeyEquivalentModifierMask_(modifiers)
                    item.setTarget_(target)
                item = file_menu.addItemWithTitle_action_keyEquivalent_("Open in Metal", "openInMetal:", "")
                item.setTarget_(target)
                item = file_menu.addItemWithTitle_action_keyEquivalent_("Retry in Metal", "retryInMetal:", "")
                item.setTarget_(target)
                api._window_menu_target = target
            except Exception as exc:
                logger(f"window menu install failed: {exc!r}")

        NSOperationQueue.mainQueue().addOperationWithBlock_(install)
    except Exception as exc:
        logger(f"window menu setup failed: {exc!r}")
