"""Sparkle 2 integration for the packaged macOS application.

Sparkle is loaded at runtime from ``Contents/Frameworks``.  Source installs
and development runs do not contain that framework, so updater setup is
deliberately best-effort and never prevents the viewer from opening.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

_DELEGATE_TYPE = None
_DRIVER_TYPE = None


class ShutdownCoordinator(Protocol):
    """The application-side hand-off Sparkle needs before it may relaunch."""

    def prepare_for_update(
        self,
        completion: Callable[[], None],
        failure: Callable[[str], None],
    ) -> bool: ...

    def cancel_preparation(self) -> None: ...


@dataclass
class SparkleHandle:
    """Objects which must remain alive for as long as the application does."""

    controller: Any
    delegate: Any
    menu_item: Any | None
    user_driver: Any = None
    card: Any = None

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
            self._install_lock = threading.RLock()
            self._pending_install_handler = None
            self._pending_install_token = None
            self._pending_install_timer = None
            return self

        # Sparkle asks this immediately before terminating and replacing the
        # application.  Retaining the block is required while the web panes
        # flush their annotations and the local server releases ND2 handles.
        def updater_shouldPostponeRelaunchForUpdate_untilInvokingBlock_(
            self, updater, item, install_handler
        ):
            with self._install_lock:
                if self._pending_install_token is not None:
                    return True
                token = self._pending_install_token = object()
                self._pending_install_handler = install_handler
            last_failure = None

            def complete():
                with self._install_lock:
                    if self._pending_install_token is not token:
                        return
                    self._pending_install_token = None
                    self._pending_install_handler = None
                    timer, self._pending_install_timer = self._pending_install_timer, None
                    if timer is not None:
                        timer.cancel()
                    # The coordinator dispatches this to AppKit's main queue.
                    # Capture this request's block, even if it re-enters us.
                    install_handler()

            def fail(message: str):
                nonlocal last_failure
                with self._install_lock:
                    if self._pending_install_token is not token:
                        return
                    if message == "update preparation is already running" or message == last_failure:
                        return
                    last_failure = message
                    self.logger(f"Sparkle relaunch postponed: {message}")

            def attempt():
                with self._install_lock:
                    if self._pending_install_token is not token:
                        return
                    self._pending_install_timer = None
                    accepted = self.coordinator.prepare_for_update(complete, fail)
                    if accepted is False and self._pending_install_token is token:
                        # An aborted worker retains ownership through recovery.
                        # Retry admission only; Sparkle's block stays on main.
                        timer = threading.Timer(0.1, attempt)
                        timer.daemon = True
                        self._pending_install_timer = timer
                        timer.start()

            attempt()
            return True

        def updaterWillRelaunchApplication_(self, updater):
            self.logger("Sparkle will relaunch application")

        def allowedChannelsForUpdater_(self, updater):
            # RC feeds must not be offered to existing stable installations.
            # Sparkle always considers its default stable channel as well, so
            # an RC installation can advance to the subsequent final release.
            from .release_selection import channel_for_version, installed_version

            return {"rc"} if channel_for_version(installed_version()) == "rc" else set()

        def updater_didAbortWithError_(self, updater, error):
            with self._install_lock:
                self._pending_install_token = None
                self._pending_install_handler = None
                timer, self._pending_install_timer = self._pending_install_timer, None
                if timer is not None:
                    timer.cancel()
                self.coordinator.cancel_preparation()
            self.logger(f"Sparkle update aborted: {error}")

    _DELEGATE_TYPE = Nd2wsiUpdaterDelegate
    return _DELEGATE_TYPE


def _register_driver_metadata(objc):
    # Objective-C encodes every block as @?; its argument signature is not in
    # the runtime method encoding. Supply it when forwarding Python wrappers
    # to SPUStandardUserDriver and when receiving the choice reply blocks.
    blocks = {
        "showUpdatePermissionRequest:reply:": (1, b"@"),
        "showUserInitiatedUpdateCheckWithCancellation:": (0, None),
        "showUpdateFoundWithAppcastItem:state:reply:": (2, b"q"),
        "showUpdateNotFoundWithError:acknowledgement:": (1, None),
        "showUpdaterError:acknowledgement:": (1, None),
        "showDownloadInitiatedWithCancellation:": (0, None),
        "showReadyToInstallAndRelaunch:": (0, b"q"),
        "showInstallingUpdateWithApplicationTerminated:retryTerminatingApplication:": (1, None),
        "showUpdateInstalledAndRelaunched:acknowledgement:": (1, None),
    }
    for selector, (index, argument) in blocks.items():
        arguments = {0: {"type": b"^v"}}
        if argument is not None:
            arguments[1] = {"type": argument}
        metadata = {"arguments": {index + 2: {"type": b"@?", "callable": {"retval": {"type": b"v"}, "arguments": arguments}}}}
        for name in (b"Nd2wsiUserDriver", b"SPUStandardUserDriver"):
            objc.registerMetaDataForSelector(name, selector.encode(), metadata)


def _driver_class(protocol):
    """Explicit Sparkle 2.9.6 selectors retain their protocol block signatures."""
    global _DRIVER_TYPE
    if _DRIVER_TYPE is not None:
        return _DRIVER_TYPE
    import objc
    from Foundation import NSObject

    _register_driver_metadata(objc)

    class Nd2wsiUserDriver(NSObject, protocols=[protocol]):
        def initWithCard_(self, card):
            self = objc.super(Nd2wsiUserDriver, self).init()
            if self is not None:
                self.card = card
                self.updater = None
            return self

        def checkForUpdates_(self, sender):
            if self.updater is not None and self.updater.canCheckForUpdates():
                self.updater.checkForUpdates()

        @objc.typedSelector(b"Z@:@")
        def validateMenuItem_(self, item):
            return self.updater is not None and bool(self.updater.canCheckForUpdates())

        def showUpdatePermissionRequest_reply_(self, request, reply):
            self.card.dispatch("showUpdatePermissionRequest_reply_", request, reply)

        def showUserInitiatedUpdateCheckWithCancellation_(self, cancellation):
            self.card.dispatch("showUserInitiatedUpdateCheckWithCancellation_", cancellation)

        def showUpdateFoundWithAppcastItem_state_reply_(self, item, state, reply):
            self.card.dispatch("showUpdateFoundWithAppcastItem_state_reply_", item, state, reply)

        def showUpdateReleaseNotesWithDownloadData_(self, data):
            self.card.dispatch("showUpdateReleaseNotesWithDownloadData_", data)

        def showUpdateReleaseNotesFailedToDownloadWithError_(self, error):
            self.card.dispatch("showUpdateReleaseNotesFailedToDownloadWithError_", error)

        def showUpdateNotFoundWithError_acknowledgement_(self, error, acknowledgement):
            self.card.dispatch("showUpdateNotFoundWithError_acknowledgement_", error, acknowledgement)

        def showUpdaterError_acknowledgement_(self, error, acknowledgement):
            self.card.dispatch("showUpdaterError_acknowledgement_", error, acknowledgement)

        def showDownloadInitiatedWithCancellation_(self, cancellation):
            self.card.dispatch("showDownloadInitiatedWithCancellation_", cancellation)

        def showDownloadDidReceiveExpectedContentLength_(self, length):
            self.card.dispatch("showDownloadDidReceiveExpectedContentLength_", length)

        def showDownloadDidReceiveDataOfLength_(self, length):
            self.card.dispatch("showDownloadDidReceiveDataOfLength_", length)

        def showDownloadDidStartExtractingUpdate(self):
            self.card.dispatch("showDownloadDidStartExtractingUpdate")

        def showExtractionReceivedProgress_(self, progress):
            self.card.dispatch("showExtractionReceivedProgress_", progress)

        def showReadyToInstallAndRelaunch_(self, reply):
            self.card.dispatch("showReadyToInstallAndRelaunch_", reply)

        def showInstallingUpdateWithApplicationTerminated_retryTerminatingApplication_(self, terminated, retry):
            self.card.dispatch("showInstallingUpdateWithApplicationTerminated_retryTerminatingApplication_", terminated, retry)

        def showUpdateInstalledAndRelaunched_acknowledgement_(self, relaunched, acknowledgement):
            self.card.dispatch("showUpdateInstalledAndRelaunched_acknowledgement_", relaunched, acknowledgement)

        def dismissUpdateInstallation(self):
            self.card.dispatch("dismissUpdateInstallation")

        def showUpdateInFocus(self):
            self.card.dispatch("showUpdateInFocus")

    _DRIVER_TYPE = Nd2wsiUserDriver
    return _DRIVER_TYPE


def install_sparkle_updater(
    *,
    shutdown_coordinator: ShutdownCoordinator,
    window=None,
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
        from Foundation import NSBundle, NSOperationQueue

        from .update_card import UpdateCardSession

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
        host = NSBundle.mainBundle()
        standard = objc.lookUpClass("SPUStandardUserDriver").alloc().initWithHostBundle_delegate_(host, None)
        if standard is None:
            raise RuntimeError("SPUStandardUserDriver initialization failed")
        def main(callback):
            NSOperationQueue.mainQueue().addOperationWithBlock_(callback)
        controller = None

        def retry():
            if (controller is None or controller.updater is None or controller.updater.sessionInProgress()
                    or not controller.updater.canCheckForUpdates()):
                return False
            controller.updater.checkForUpdates()
            return True

        def open_notes(url):
            import webbrowser

            threading.Thread(target=webbrowser.open, args=(url,), name="update-release-notes", daemon=True).start()

        card = UpdateCardSession(standard, lambda: window, main, retry=retry, open_notes=open_notes)
        controller = _driver_class(objc.protocolNamed("SPUUserDriver")).alloc().initWithCard_(card)
        # Sparkle has no generated PyObjC metadata: preserve the NSError out
        # parameter so a failed start returns its diagnostic as well as BOOL.
        objc.registerMetaDataForSelector(b"SPUUpdater", b"startUpdater:",
                                         {"arguments": {2: {"type": b"o^@"}}})
        updater = objc.lookUpClass("SPUUpdater").alloc().initWithHostBundle_applicationBundle_userDriver_delegate_(
            host, host, controller, delegate)
        if updater is None:
            raise RuntimeError("SPUUpdater initialization failed")
        controller.updater = updater
        started = updater.startUpdater_(None)
        success = started[0] if isinstance(started, tuple) else started
        if not success:
            error = started[1] if isinstance(started, tuple) and len(started) > 1 else None
            if error is not None:
                logger(f"Sparkle startUpdater failed: {error}")
                standard.showUpdaterError_acknowledgement_(error, lambda: None)
            raise RuntimeError(f"SPUUpdater could not start: {error or 'no error details returned'}")
        if window is not None:
            def end_page(*_args):
                main(card.page_closed)
            window.events.before_load += end_page
            window.events.closed += end_page
        menu_item = _install_menu_item(controller)
        logger("Sparkle updater started")
        return SparkleHandle(controller=controller, delegate=delegate, menu_item=menu_item,
                             user_driver=controller, card=card)
    except Exception as exc:
        logger(f"Sparkle unavailable: {type(exc).__name__}: {exc}")
        return None
