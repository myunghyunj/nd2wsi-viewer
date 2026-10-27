"""Coordinate saved browser state with native window and updater shutdown.

The shell supplies a bridge, a window, and a logger. This module owns request
identity, cancellation, annotation acknowledgements, and the worker/UI-thread
ordering shared by native Close (AppKit and WinForms) and Sparkle. It does not
create windows or import the app entry point; native frameworks (Foundation on
macOS, pythonnet's System on Windows) are loaded only at dispatch time.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from collections.abc import Callable
from typing import Protocol


class _ViewerNotAnswering(RuntimeError):
    """The viewer never ran the save handshake or never replied in time.

    Distinct from a definite refusal (an unresolved conflict, a slide still
    opening, an export still running), which the user resolves in the viewer.
    """


class LifecycleApi(Protocol):
    """The window-owned server state needed to guard resource teardown."""

    _httpd: object | None
    _frac: float
    _status: str

    def update_block_reason(self) -> str | None: ...

    def stop_server_for_update(self) -> None: ...


class LifecycleWindow(Protocol):
    """The pywebview operations used by save-before-close coordination."""

    native: object  # NSWindow on macOS, the WinForms Form on Windows

    def evaluate_js(self, script: str, callback: Callable | None = None) -> object: ...

    def destroy(self) -> None: ...


class _ShutdownOwnership:
    """Which coordinator's request may freeze this window's panes.

    Native close and Sparkle preparation drive the same shell, which remembers
    one preparation request id. Two requests at once overwrite it, and the
    loser's recovery unlocks panes the other still needs frozen, for example
    while it tears down the server. Every coordinator of a window's api claims
    this record before it starts a request and releases it when the request
    ends. It never calls out, so it is safe to use under a coordinator's lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._holder: tuple[object, str] | None = None

    def claim(self, coordinator: object, request_id: str) -> bool:
        """Own the panes for ``request_id`` unless another coordinator does."""
        with self._lock:
            if self._holder is not None and self._holder[0] is not coordinator:
                return False
            self._holder = (coordinator, request_id)
            return True

    def held_by_other(self, coordinator: object) -> bool:
        with self._lock:
            return self._holder is not None and self._holder[0] is not coordinator

    def release(self, coordinator: object, request_id: str) -> None:
        """Give the panes up, but only from the request that holds them."""
        with self._lock:
            if self._holder is not None and self._holder[0] is coordinator and self._holder[1] == request_id:
                self._holder = None


_SHUTDOWN_OWNERSHIP_LOCK = threading.Lock()


class UpdateShutdownCoordinator:
    """Flush browser work and release native resources before Sparkle relaunches."""

    _FLUSH_TIMEOUT = 30.0
    _RETRY_DELAY = 5.0
    _DRAIN_POLL = 0.5
    # Notices and editing recovery are tiny scripts; a viewer that cannot run
    # one this quickly is not answering, and the worker must move on.
    _BROWSER_CALL_TIMEOUT = 5.0

    def __init__(self, api: LifecycleApi, window: LifecycleWindow, *, logger: Callable[[str], None]):
        self.api = api
        self._log = logger
        self.window = window
        self._lock = threading.Lock()
        self._running = False
        self._request_id = ""
        self._cancelled = threading.Event()
        self._wire_request_id = ""
        self._teardown_started = False
        self._install_guard = None
        self._browser_tail: threading.Event | None = None  # the latest browser call's completion

    def prepare_for_update(self, completion, failure) -> bool:
        with self._lock:
            already_running = self._running
            request_id = uuid.uuid4().hex
            # A native close that is saving this window owns its panes; its
            # recovery would unlock them in the middle of this preparation.
            window_closing = not already_running and not self._ownership().claim(self, request_id)
            if not (already_running or window_closing):
                self._running = True
                self._request_id = request_id
                cancelled = self._cancelled = threading.Event()
                self._wire_request_id = ""
                self._teardown_started = False
        if already_running:
            # Callbacks may re-enter the coordinator; never call one under its lock.
            failure("update preparation is already running")
            return False
        if window_closing:
            failure("the window is closing; update preparation waits until the close finishes")
            return False
        # Sparkle invokes its delegate on AppKit's main thread. Even with a
        # promise callback, pywebview.evaluate_js queues work to that thread and
        # synchronously waits for it. Doing any preparation here deadlocks it.
        worker = threading.Thread(
            target=self._prepare_worker,
            args=(request_id, cancelled, completion, failure),
            name="updater-safe-relaunch",
            daemon=True,
        )
        try:
            worker.start()
        except Exception as exc:
            self._finish_request(request_id)
            failure(f"Could not start update preparation: {exc}")
            return False
        return True

    def cancel_preparation(self) -> None:
        """Invalidate retries and queued completions after Sparkle aborts."""
        with self._lock:
            self._cancelled.set()
        # Only the worker releases ownership, after any in-flight teardown
        # finishes and browser recovery has been sent (and answered, unless the
        # viewer is not responding). A new installer must not overtake old I/O.

    def _dispatch_to_ui(self, operation: Callable[[], None]) -> None:
        """Queue ``operation`` on the native UI thread that owns the window.

        Workers never call native window or Sparkle APIs themselves: AppKit
        work goes to the main operation queue, WinForms work to the Form's
        message loop. Native frameworks are imported only here.
        """
        def guarded():
            # Never let a Python error escape into the native event loop.
            try:
                operation()
            except Exception as exc:
                self._log(f"native UI operation failed: {exc!r}")

        if sys.platform == "darwin":
            from Foundation import NSOperationQueue

            NSOperationQueue.mainQueue().addOperationWithBlock_(guarded)
        elif sys.platform == "win32":
            from System import Action

            native = self.window.native
            if native.InvokeRequired:
                native.BeginInvoke(Action(guarded))
            else:
                guarded()
        else:
            raise RuntimeError(f"No native UI thread is available on {sys.platform}")

    def _start_browser_call(self, script: str, callback: Callable | None = None, *, wait: float = 0.0,
                            cancelled: threading.Event | None = None, queue: bool = False):
        """Run ``window.evaluate_js`` on its own daemon thread, after the last call.

        pywebview's evaluate_js waits without a deadline: WinForms for its
        ExecuteScriptAsync continuation, Cocoa for a main-queue block, and
        every backend first up to 20 s for the page bridge, then raises. A
        hung, crashed or never-initialised viewer must cost the worker a
        bounded wait, never the request it owns.

        Calls reach the viewer one at a time and in order: each starts only
        once the previous one returned. A ``queue``d call (editing recovery)
        is always issued and waits on its own thread, so a viewer that
        recovers still runs it after the script it undoes. Any other call is
        dropped if the previous one is still waiting after ``wait`` seconds
        (or once ``cancelled`` is set), so a hung viewer strands one thread
        rather than one per retry. Returns the call's ``done`` event and the
        list its exception is appended to, or None when it was dropped.
        """
        with self._lock:
            previous = self._browser_tail
        if previous is not None and not queue:
            deadline = time.monotonic() + wait
            while not previous.wait(min(0.05, max(0.0, deadline - time.monotonic()))):
                if (cancelled is not None and cancelled.is_set()) or time.monotonic() >= deadline:
                    return None
        done = threading.Event()
        errors: list[Exception] = []

        def call():
            try:
                if queue and previous is not None:
                    previous.wait()
                if callback is None:
                    self.window.evaluate_js(script)
                else:
                    self.window.evaluate_js(script, callback=callback)
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()

        threading.Thread(target=call, name="window-evaluate-js", daemon=True).start()
        with self._lock:
            self._browser_tail = done
        return done, errors

    def _browser_call(self, script: str, what: str, *, queue: bool = False) -> None:
        """Run a fire-and-forget script, waiting at most _BROWSER_CALL_TIMEOUT."""
        deadline = time.monotonic() + self._BROWSER_CALL_TIMEOUT
        try:
            started = self._start_browser_call(script, wait=self._BROWSER_CALL_TIMEOUT, queue=queue)
        except Exception as exc:
            self._log(f"{what} failed: {exc!r}")
            return
        if started is None or not started[0].wait(max(0.0, deadline - time.monotonic())):
            self._log(f"{what} failed: the viewer did not answer within {self._BROWSER_CALL_TIMEOUT:g} seconds")
        elif started[1]:
            self._log(f"{what} failed: {started[1][0]!r}")

    def _is_current(self, request_id, cancelled) -> bool:
        with self._lock:
            return self._request_id == request_id and not cancelled.is_set()

    def _ownership(self) -> _ShutdownOwnership:
        """The pane ownership shared by every coordinator of this api.

        It is attached to the api on first use, so the close guard and the
        Sparkle coordinator, which are built separately, find the same record.
        """
        with _SHUTDOWN_OWNERSHIP_LOCK:
            ownership = getattr(self.api, "_shutdown_ownership", None)
            if not isinstance(ownership, _ShutdownOwnership):
                ownership = self.api._shutdown_ownership = _ShutdownOwnership()
            return ownership

    def _finish_request(self, request_id) -> None:
        with self._lock:
            if self._request_id == request_id:
                self._request_id = ""
                self._running = False
                self._wire_request_id = ""
            self._ownership().release(self, request_id)

    def _recover_browser(self, request_id) -> None:
        with self._lock:
            if self._request_id != request_id:
                return
            wire_id = self._wire_request_id
            after_teardown = self._teardown_started
        if not wire_id:
            return
        # Queued behind any script the viewer has not finished, so it unlocks
        # editing even if the viewer recovers later. The shell ignores a
        # cancellation whose wire id is no longer current, so one that arrives
        # late cannot unlock a newer request.
        self._browser_call(
            "window.nd2wsiCancelUpdate && window.nd2wsiCancelUpdate("
            f"{json.dumps(wire_id)}, {json.dumps(after_teardown)})",
            "update cancellation recovery",
            queue=True,
        )

    def _notice(self, message: str) -> None:
        payload = json.dumps(str(message))
        self._browser_call(
            f"window.nd2wsiUpdateNotice && window.nd2wsiUpdateNotice({payload})",
            "update notice",
        )

    def _report_wait(self, message: str, failure) -> None:
        self._notice(message)
        failure(message)

    def _flush_panes(self, request_id, cancelled):
        # Every retry has a distinct wire id so a late iframe reply from an
        # earlier timed-out attempt cannot satisfy the new attempt.
        wire_id = f"{request_id}-{uuid.uuid4().hex}"
        with self._lock:
            if self._request_id != request_id or cancelled.is_set():
                return None
        request = json.dumps(wire_id)
        script = (
            "typeof window.nd2wsiPrepareForUpdate === 'function' "
            f"? window.nd2wsiPrepareForUpdate({request}) "
            f": Promise.resolve({self._missing_flush_result()})"
        )
        answered = threading.Event()
        results = []

        def after_flush(result):
            # This callback can arrive on any thread, or after cancellation.
            # Each attempt owns its result/event, so late replies cannot satisfy
            # a later attempt. It never calls a blocking browser API itself.
            if self._is_current(request_id, cancelled):
                results.append(result)
                answered.set()

        # The deadline covers the browser call itself: a viewer that never
        # runs the script is as unconfirmed as one that never replies.
        deadline = time.monotonic() + self._FLUSH_TIMEOUT
        started = self._start_browser_call(script, after_flush, wait=self._FLUSH_TIMEOUT, cancelled=cancelled)
        if started is None:  # cancelled, or an earlier script is still stuck in this viewer
            if cancelled.is_set():
                return None
            return {"ok": False, "unanswered": True, "error": "the viewer did not confirm its saved state"}
        with self._lock:
            # Recovery cancels the last preparation the viewer was actually sent.
            if self._request_id == request_id:
                self._wire_request_id = wire_id
        _, errors = started
        while not answered.is_set():
            if cancelled.wait(min(0.05, max(0, deadline - time.monotonic()))):
                return None
            if answered.is_set():
                break
            if errors:
                raise errors[0]  # for example, the page bridge never became ready
            if time.monotonic() >= deadline:
                return {"ok": False, "unanswered": True, "error": "the viewer did not confirm its saved state"}
        return results[0]

    def _missing_flush_result(self) -> str:
        return "{ok:true, panes:0}"

    def _prepare_worker(self, request_id, cancelled, completion, failure) -> None:
        completed = threading.Event()
        completion_errors = []
        try:
            self._log("Sparkle shutdown preparation started in background")
            while self._is_current(request_id, cancelled):
                try:
                    result = self._flush_panes(request_id, cancelled)
                except Exception as exc:
                    result = {"ok": False, "error": f"Could not prepare the viewer: {exc}"}
                if not self._is_current(request_id, cancelled):
                    return
                if isinstance(result, dict) and result.get("ok"):
                    break
                message = (result.get("error") if isinstance(result, dict) else None)
                self._report_wait(
                    f"Update waiting: {message or 'annotations were not saved'}; retrying…",
                    failure,
                )
                if cancelled.wait(self._RETRY_DELAY):
                    return
            else:
                return
            self._log("Sparkle shutdown: annotations saved")
            if not self._drain_and_close(request_id, cancelled):
                return
            def finish():
                if not self._is_current(request_id, cancelled):
                    return
                try:
                    self._log("Sparkle shutdown: resources released; resuming installer")
                    # Preparation ends here. Resuming makes Sparkle send this
                    # app a quit, which arrives as a native close; it must
                    # reach the close guard exactly as before, not the veto.
                    self._ownership().release(self, request_id)
                    completion()
                except Exception as exc:
                    completion_errors.append(exc)
                finally:
                    completed.set()

            # Never fall back to invoking Sparkle on a worker thread.
            self._dispatch_to_ui(finish)
            while not completed.wait(0.05):
                if cancelled.is_set():
                    return
            if completion_errors:
                raise completion_errors[0]
            # The continuation starts installation; it does not prove Sparkle
            # has terminated us. Keep the saved-state token and worker alive
            # so a later native abort can unlock panes even after this return.
            cancelled.wait()
        except Exception as exc:
            if self._is_current(request_id, cancelled):
                message = f"Update could not close the viewer safely: {exc}"
                self._log(message)
                self._report_wait(message, failure)
        finally:
            if cancelled.is_set() or not completed.is_set() or completion_errors:
                self._recover_browser(request_id)
            if self._install_guard is not None:
                self._install_guard.release()
                self._install_guard = None
            self._finish_request(request_id)

    def _drain_and_close(self, request_id, cancelled) -> bool:
        session = getattr(self.api, "_window_session", None)
        if session is not None:
            from .update_guard import UpdateInstallGuard

            self._install_guard = UpdateInstallGuard(session)
        last_reason = None
        while self._is_current(request_id, cancelled):
            reason = self.api.update_block_reason()
            if reason is None and self._install_guard is not None:
                reason = self._install_guard.acquire()
            if reason is None:
                break
            if reason != last_reason:
                self._notice(reason)
                last_reason = reason
            if cancelled.wait(self._DRAIN_POLL):
                return False
        with self._lock:
            if self._request_id != request_id or cancelled.is_set():
                return False
            self._teardown_started = True
        self.api.stop_server_for_update()
        return self._is_current(request_id, cancelled)


class WindowCloseCoordinator(UpdateShutdownCoordinator):
    """Veto native close until this window confirms saved annotations.

    pywebview raises closing on the toolkit's UI thread (AppKit's
    windowShouldClose:, WinForms' FormClosing); no browser call can block
    there. The existing save protocol freezes editing while a worker awaits
    acknowledgements and active exports, then hands the approved close back to
    the UI thread. Failure restores editing and leaves the window open.

    A viewer that cannot answer (hung, crashed or never initialised) must not
    make the window impossible to close: a page whose bridge never became
    ready while no slide is open has nothing to save and closes at once, and a
    close whose handshake goes unanswered right after another unanswered one
    asks the user natively whether to close without confirmed saves. A viewer
    that answers with a refusal (conflict, slide opening, export running)
    keeps the existing behaviour: the window stays open with a notice.

    Sparkle preparation and this guard never own the panes at once. While an
    update is being prepared, a close is vetoed with a notice; an approved or
    forced close still goes through, and so does the quit Sparkle sends once
    the installer resumes.
    """

    _DRAIN_TIMEOUT = 30.0
    _UNCONFIRMED_CLOSE_TITLE = "Close without confirmed saves?"
    _UPDATE_PREPARING_NOTICE = "An update is being prepared. Cancel it in the update window to close."

    def __init__(self, api: LifecycleApi, window: LifecycleWindow, *, logger: Callable[[str], None]):
        super().__init__(api, window, logger=logger)
        self._approved = False
        self._forced = False
        self._failed_closes = 0  # consecutive handshakes the viewer did not answer
        self._close_listeners: list[Callable[[], None]] = []

    def approve_close(self, reason: str) -> None:
        """Let native close proceed without the save handshake.

        Only for automated diagnostics: a GUI smoke that failed, or whose viewer
        never became ready, cannot answer the save protocol and must not hang
        on the veto. A running request is cancelled; its worker then releases
        ownership without touching the closing browser.
        """
        with self._lock:
            self._forced = True
            self._cancelled.set()
        self._log(f"Native close approved without save confirmation: {reason}")

    def add_close_listener(self, listener: Callable[[], None]) -> None:
        """Run ``listener`` on the UI thread after a confirmed close is handed
        to the window, before the GUI loop can return."""
        with self._lock:
            self._close_listeners.append(listener)

    def request_native_close(self) -> None:
        """Start the close a title-bar button would, so the veto runs.

        pywebview's Cocoa destroy() is NSWindow.close, which skips
        windowShouldClose:; performClose: is the close button's own action.
        Its WinForms destroy() is Form.Close, which raises FormClosing exactly
        like the title-bar X, Alt+F4 or a taskbar close.
        """
        if sys.platform == "darwin":
            self._dispatch_to_ui(lambda: self.window.native.performClose_(None))
        else:
            self.window.destroy()

    def on_closing(self) -> bool:
        # Runs on the UI thread; reads plain attributes only, never blocks.
        idle_page = self._page_never_ready_without_slides()
        with self._lock:
            if self._approved or self._forced:
                return True
            if self._running:
                return False
            ownership = self._ownership()
            request_id = uuid.uuid4().hex
            # Sparkle preparation owns the panes until it resumes the
            # installer. An idle page starts no request, so it only checks.
            preparing_update = (ownership.held_by_other(self) if idle_page
                                else not ownership.claim(self, request_id))
            if preparing_update:
                approved = False
            elif idle_page:
                approved = True
            else:
                approved = False
                self._running = True
                self._request_id = request_id
                cancelled = self._cancelled = threading.Event()
                self._wire_request_id = ""
                self._teardown_started = False
                ask_to_force = self._failed_closes > 0
        if preparing_update:
            self._refuse_close_while_preparing_update()
            return False
        if approved:
            self._log("Native close allowed: the page never became ready and no slide is open")
            return True
        try:
            threading.Thread(target=self._close_worker, args=(request_id, cancelled, ask_to_force),
                             name="window-safe-close", daemon=True).start()
        except Exception as exc:
            self._finish_request(request_id)
            self.api._status = f"Window remains open: could not begin saving ({exc})."
            self._log(self.api._status)
        return False

    def _refuse_close_while_preparing_update(self) -> None:
        """Keep the window open, and say why, while Sparkle preparation owns it.

        Runs on the UI thread, so the notice is sent from its own thread. The
        panes stay frozen for the update; cancelling it in Sparkle's window
        unlocks them, and the next close then runs the save handshake.
        """
        message = self._UPDATE_PREPARING_NOTICE
        self.api._status = message
        self._log(f"Window remains open: {message}")
        try:
            threading.Thread(target=self._notice, args=(message,),
                             name="window-close-notice", daemon=True).start()
        except Exception as exc:
            self._log(f"update notice failed: {exc!r}")

    def _holds_no_slides(self) -> bool:
        server = self.api._httpd
        slides = getattr(getattr(server, "registry", None), "slides", None)
        return self.api._frac < 0 and (server is None or slides is not None and not slides)

    def _page_never_ready_without_slides(self) -> bool:
        """True while pywebview's page bridge is not ready and no slide is open.

        Edits belong to open slides, so nothing can be unsaved. This is also
        the state a failed WebView2 initialisation leaves behind, where every
        browser call would wait 20 s and then fail. A window without that
        pywebview event is treated as ready, so the handshake still runs.
        """
        ready = getattr(getattr(self.window, "events", None), "_pywebviewready", None)
        is_set = getattr(ready, "is_set", None)
        return callable(is_set) and is_set() is False and self._holds_no_slides()

    def _missing_flush_result(self) -> str:
        if self._holds_no_slides():
            return "{ok:true, panes:0}"
        return "{ok:false, error:'the viewer is not ready to confirm saved annotations'}"

    def _confirm_saved(self, request_id, cancelled) -> bool:
        """Flush the panes and drain active exports.

        Returns False when the request was cancelled or superseded, and raises
        when the viewer did not confirm its saved state in time.
        """
        try:
            result = self._flush_panes(request_id, cancelled)
        except Exception as exc:  # the browser call itself failed (bridge never ready, crash)
            raise _ViewerNotAnswering(str(exc) or "the viewer did not confirm its saved state") from exc
        if not self._is_current(request_id, cancelled):
            return False
        if not isinstance(result, dict) or not result.get("ok"):
            reason = result.get("error") if isinstance(result, dict) else None
            if isinstance(result, dict) and result.get("unanswered"):
                raise _ViewerNotAnswering(reason or "the viewer did not confirm its saved state")
            raise RuntimeError(reason or "annotations were not confirmed saved")
        deadline = time.monotonic() + self._DRAIN_TIMEOUT
        while self._is_current(request_id, cancelled):
            reason = self.api.update_block_reason()
            if reason is None:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(reason + " Close was cancelled; try again when it finishes.")
            if cancelled.wait(min(self._DRAIN_POLL, max(0, deadline - time.monotonic()))):
                return False
        return self._is_current(request_id, cancelled)

    def _ask_to_close_unconfirmed(self, reason) -> bool:
        """Ask natively, from this worker, whether to close without confirmed saves.

        pywebview shows the dialog itself (a MessageBox on WinForms, an NSAlert
        through the main queue on Cocoa) and blocks only this worker until it
        is answered; it needs no response from the viewer. Only an explicit OK
        counts; a missing or failing dialog keeps the window open.
        """
        ask = getattr(self.window, "create_confirmation_dialog", None)
        if not callable(ask):
            return False
        message = (
            f"{self._UNCONFIRMED_CLOSE_TITLE}\n\n"
            f"The viewer is not responding, so this window's annotations and settings could not be confirmed as saved ({reason}).\n\n"
            "Choose OK to close the window anyway. Changes that were not saved, and any export "
            "still running, will be lost. Choose Cancel to keep it open."
        )
        try:
            return ask(self._UNCONFIRMED_CLOSE_TITLE, message) is True
        except Exception as exc:
            self._log(f"close confirmation dialog failed: {exc!r}")
            return False

    def _close_worker(self, request_id, cancelled, ask_to_force=False) -> None:
        closing = threading.Event()
        failures = []
        try:
            try:
                if not self._confirm_saved(request_id, cancelled):
                    return
            except Exception as exc:
                # The user's way out of a viewer that cannot answer: offered only
                # when this handshake went unanswered right after another one did.
                # A definite refusal (conflict, slide opening, export running) is
                # resolved in the viewer and never offers to discard work; a first
                # attempt never asks, so automation that closes once never sees it.
                if not (isinstance(exc, _ViewerNotAnswering) and ask_to_force
                        and self._is_current(request_id, cancelled)
                        and self._ask_to_close_unconfirmed(exc)):
                    raise
                self._log(f"Closing without confirmed saves at the user's request: {exc}")
                if not self._is_current(request_id, cancelled):
                    return

            def finish():
                # Runs on the UI thread. Identity and approval are decided under
                # one lock, so a cancelled or superseded request never approves.
                with self._lock:
                    current = self._request_id == request_id and not cancelled.is_set()
                    if current:
                        self._approved = True
                        self._failed_closes = 0
                        listeners = list(self._close_listeners)
                if not current:
                    closing.set()
                    return
                try:
                    # WinForms re-raises FormClosing here; it is now approved.
                    self.window.destroy()
                except Exception as exc:
                    with self._lock:
                        self._approved = False
                    failures.append(exc)
                else:
                    for listener in listeners:
                        try:
                            listener()
                        except Exception as exc:
                            self._log(f"close listener failed: {exc!r}")
                finally:
                    closing.set()

            self._dispatch_to_ui(finish)
            while not closing.wait(0.05):
                if cancelled.is_set():
                    return
            if failures:
                raise failures[0]
        except Exception as exc:
            message = f"Window remains open: {exc}"
            with self._lock:
                # Count consecutive unanswered handshakes only; any answer resets.
                self._failed_closes = self._failed_closes + 1 if isinstance(exc, _ViewerNotAnswering) else 0
            self.api._status = message
            self._log(message)
            self._notice(message)
        finally:
            if not (self._approved or self._forced):
                self._recover_browser(request_id)
            self._finish_request(request_id)
