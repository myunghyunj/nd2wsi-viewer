"""Coordinate saved browser state with native window and updater shutdown.

The shell supplies a bridge, a window, and a logger. This module owns request
identity, cancellation, annotation acknowledgements, and the worker/main-thread
ordering shared by native Close and Sparkle. It does not create windows or
import the app entry point; native frameworks are loaded only at dispatch time.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable
from typing import Protocol


class LifecycleApi(Protocol):
    """The window-owned server state needed to guard resource teardown."""

    _httpd: object | None
    _frac: float
    _status: str

    def update_block_reason(self) -> str | None: ...

    def stop_server_for_update(self) -> None: ...


class LifecycleWindow(Protocol):
    """The pywebview operations used by save-before-close coordination."""

    def evaluate_js(self, script: str, callback: Callable | None = None) -> object: ...

    def destroy(self) -> None: ...


class UpdateShutdownCoordinator:
    """Flush browser work and release native resources before Sparkle relaunches."""

    _FLUSH_TIMEOUT = 30.0
    _RETRY_DELAY = 5.0
    _DRAIN_POLL = 0.5

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

    def prepare_for_update(self, completion, failure) -> bool:
        with self._lock:
            already_running = self._running
            if not already_running:
                self._running = True
                request_id = self._request_id = uuid.uuid4().hex
                cancelled = self._cancelled = threading.Event()
                self._wire_request_id = ""
                self._teardown_started = False
        if already_running:
            # Callbacks may re-enter the coordinator; never call one under its lock.
            failure("update preparation is already running")
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
        # Only the worker releases ownership, after any in-flight teardown and
        # browser recovery finish. A new installer must not overtake old I/O.

    def _is_current(self, request_id, cancelled) -> bool:
        with self._lock:
            return self._request_id == request_id and not cancelled.is_set()

    def _finish_request(self, request_id) -> None:
        with self._lock:
            if self._request_id == request_id:
                self._request_id = ""
                self._running = False
                self._wire_request_id = ""

    def _recover_browser(self, request_id) -> None:
        with self._lock:
            if self._request_id != request_id:
                return
            wire_id = self._wire_request_id
            after_teardown = self._teardown_started
        if not wire_id:
            return
        try:
            self.window.evaluate_js(
                "window.nd2wsiCancelUpdate && window.nd2wsiCancelUpdate("
                f"{json.dumps(wire_id)}, {json.dumps(after_teardown)})"
            )
        except Exception as exc:
            self._log(f"update cancellation recovery failed: {exc!r}")

    def _notice(self, message: str) -> None:
        try:
            payload = json.dumps(str(message))
            self.window.evaluate_js(
                f"window.nd2wsiUpdateNotice && window.nd2wsiUpdateNotice({payload})"
            )
        except Exception as exc:
            self._log(f"update notice failed: {exc!r}")

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
            self._wire_request_id = wire_id
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

        self.window.evaluate_js(script, callback=after_flush)
        deadline = time.monotonic() + self._FLUSH_TIMEOUT
        while not answered.is_set():
            if cancelled.wait(min(0.05, max(0, deadline - time.monotonic()))):
                return None
            if not answered.is_set() and time.monotonic() >= deadline:
                return {"ok": False, "error": "the viewer did not confirm its saved state"}
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
            from Foundation import NSOperationQueue

            def finish():
                if not self._is_current(request_id, cancelled):
                    return
                try:
                    self._log("Sparkle shutdown: resources released; resuming installer")
                    completion()
                except Exception as exc:
                    completion_errors.append(exc)
                finally:
                    completed.set()

            # Never fall back to invoking Sparkle on a worker thread.
            NSOperationQueue.mainQueue().addOperationWithBlock_(finish)
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

    Cocoa invokes closing on its main thread; no browser call can block there.
    The existing save protocol freezes editing while a worker awaits acknowledg-
    ments and active exports. Failure restores editing and leaves the window open.
    """

    _DRAIN_TIMEOUT = 30.0

    def __init__(self, api: LifecycleApi, window: LifecycleWindow, *, logger: Callable[[str], None]):
        super().__init__(api, window, logger=logger)
        self._approved = False

    def on_closing(self) -> bool:
        with self._lock:
            if self._approved:
                return True
            if self._running:
                return False
            self._running = True
            request_id = self._request_id = uuid.uuid4().hex
            cancelled = self._cancelled = threading.Event()
            self._wire_request_id = ""
            self._teardown_started = False
        try:
            threading.Thread(target=self._close_worker, args=(request_id, cancelled),
                             name="window-safe-close", daemon=True).start()
        except Exception as exc:
            self._finish_request(request_id)
            self.api._status = f"Window remains open: could not begin saving ({exc})."
            self._log(self.api._status)
        return False

    def _missing_flush_result(self) -> str:
        server = self.api._httpd
        slides = getattr(getattr(server, "registry", None), "slides", None)
        if self.api._frac < 0 and (server is None or slides is not None and not slides):
            return "{ok:true, panes:0}"
        return "{ok:false, error:'the viewer is not ready to confirm saved annotations'}"

    def _close_worker(self, request_id, cancelled) -> None:
        closing = threading.Event()
        failures = []
        try:
            result = self._flush_panes(request_id, cancelled)
            if not self._is_current(request_id, cancelled):
                return
            if not isinstance(result, dict) or not result.get("ok"):
                reason = result.get("error") if isinstance(result, dict) else None
                raise RuntimeError(reason or "annotations were not confirmed saved")
            deadline = time.monotonic() + self._DRAIN_TIMEOUT
            while self._is_current(request_id, cancelled):
                reason = self.api.update_block_reason()
                if reason is None:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(reason + " Close was cancelled; try again when it finishes.")
                if cancelled.wait(min(self._DRAIN_POLL, max(0, deadline - time.monotonic()))):
                    return
            if not self._is_current(request_id, cancelled):
                return
            from Foundation import NSOperationQueue

            def finish():
                if not self._is_current(request_id, cancelled):
                    closing.set()
                    return
                try:
                    with self._lock:
                        self._approved = True
                    self.window.destroy()
                except Exception as exc:
                    with self._lock:
                        self._approved = False
                    failures.append(exc)
                finally:
                    closing.set()

            NSOperationQueue.mainQueue().addOperationWithBlock_(finish)
            while not closing.wait(0.05):
                if cancelled.is_set():
                    return
            if failures:
                raise failures[0]
        except Exception as exc:
            message = f"Window remains open: {exc}"
            self.api._status = message
            self._log(message)
            self._notice(message)
        finally:
            if not self._approved:
                self._recover_browser(request_id)
            self._finish_request(request_id)
