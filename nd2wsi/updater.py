"""Sparkle 2 integration for the packaged macOS application.

Sparkle is loaded at runtime from ``Contents/Frameworks``.  Source installs
and development runs do not contain that framework, so updater setup is
deliberately best-effort and never prevents the viewer from opening.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

_DELEGATE_TYPE = None


class ShutdownCoordinator(Protocol):
    """The application-side hand-off Sparkle needs before it may relaunch."""

    def prepare_for_update(
        self,
        completion: Callable[[], None],
        failure: Callable[[str], None],
    ) -> None: ...


@dataclass
class SparkleHandle:
    """Objects which must remain alive for as long as the application does."""

    controller: Any
    delegate: Any
    menu_item: Any | None

    def check_for_updates(self) -> None:
        self.controller.checkForUpdates_(None)


def _framework_path() -> Path:
    from Foundation import NSBundle

    bundle = Path(str(NSBundle.mainBundle().bundlePath()))
    return bundle / "Contents" / "Frameworks" / "Sparkle.framework"


def _install_menu_item(controller: Any) -> Any | None:
    """Add the standard updater command to the macOS application menu."""
    from AppKit import NSApplication, NSMenu, NSMenuItem
    from Foundation import NSSelectorFromString

    app = NSApplication.sharedApplication()
    main = app.mainMenu()
    if main is None:
        main = NSMenu.alloc().initWithTitle_("")
        app.setMainMenu_(main)

    app_item = main.itemAtIndex_(0) if main.numberOfItems() else None
    if app_item is None:
        app_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("", None, "")
        main.addItem_(app_item)
    submenu = app_item.submenu()
    if submenu is None:
        submenu = NSMenu.alloc().initWithTitle_("nd2wsi-viewer")
        app_item.setSubmenu_(submenu)

    title = "Check for Updates…"
    for item in submenu.itemArray():
        if str(item.title()) == title:
            item.setTarget_(controller)
            item.setAction_(NSSelectorFromString("checkForUpdates:"))
            return item

    if submenu.numberOfItems():
        submenu.addItem_(NSMenuItem.separatorItem())
    item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        title,
        NSSelectorFromString("checkForUpdates:"),
        "",
    )
    item.setTarget_(controller)
    submenu.addItem_(item)
    return item


def _delegate_class(protocol: Any):
    global _DELEGATE_TYPE
    if _DELEGATE_TYPE is not None:
        return _DELEGATE_TYPE
    import objc
    from Foundation import NSObject

    class Nd2wsiUpdaterDelegate(NSObject, protocols=[protocol]):
        coordinator: ShutdownCoordinator
        logger: Callable[[str], None]

        def initWithCoordinator_logger_(self, coordinator, logger):
            self = objc.super(Nd2wsiUpdaterDelegate, self).init()
            if self is None:
                return None
            self.coordinator = coordinator
            self.logger = logger
            self._pending_install_handler = None
            return self

        # Sparkle asks this immediately before terminating and replacing the
        # application.  Retaining the block is required while the web panes
        # flush their annotations and the local server releases ND2 handles.
        def updater_shouldPostponeRelaunchForUpdate_untilInvokingBlock_(
            self, updater, item, install_handler
        ):
            self._pending_install_handler = install_handler

            def complete():
                handler = self._pending_install_handler
                self._pending_install_handler = None
                if handler is not None:
                    handler()

            def fail(message: str):
                self.logger(f"Sparkle relaunch postponed: {message}")

            self.coordinator.prepare_for_update(complete, fail)
            return True

        def updaterWillRelaunchApplication_(self, updater):
            self.logger("Sparkle will relaunch application")

        def updater_didAbortWithError_(self, updater, error):
            self._pending_install_handler = None
            self.logger(f"Sparkle update aborted: {error}")

    _DELEGATE_TYPE = Nd2wsiUpdaterDelegate
    return _DELEGATE_TYPE


def install_sparkle_updater(
    *,
    shutdown_coordinator: ShutdownCoordinator,
    logger: Callable[[str], None] = lambda _message: None,
) -> SparkleHandle | None:
    """Load the embedded framework and start Sparkle's scheduled checks.

    This function must run on AppKit's main thread.  The caller retains the
    returned handle so the controller, delegate, and native menu item stay
    alive for the application's lifetime.
    """

    if sys.platform != "darwin":
        return None
    try:
        import objc
        from Foundation import NSBundle

        path = _framework_path()
        if not path.is_dir():
            raise FileNotFoundError(path)
        bundle = NSBundle.bundleWithPath_(str(path))
        if bundle is None or not bundle.load():
            raise RuntimeError(f"could not load {path}")

        protocol = objc.protocolNamed("SPUUpdaterDelegate")
        delegate_type = _delegate_class(protocol)
        delegate = delegate_type.alloc().initWithCoordinator_logger_(
            shutdown_coordinator, logger
        )
        controller_type = objc.lookUpClass("SPUStandardUpdaterController")
        controller = (
            controller_type.alloc()
            .initWithStartingUpdater_updaterDelegate_userDriverDelegate_(
                True, delegate, None
            )
        )
        if controller is None:
            raise RuntimeError("SPUStandardUpdaterController initialization failed")
        menu_item = _install_menu_item(controller)
        logger("Sparkle updater started")
        return SparkleHandle(controller=controller, delegate=delegate, menu_item=menu_item)
    except Exception as exc:
        logger(f"Sparkle unavailable: {type(exc).__name__}: {exc}")
        return None
