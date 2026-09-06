"""Exercise updater preparation across real worker/main-thread boundaries.

The synthetic webview has the same critical Cocoa shape as pywebview: enqueue
JavaScript on the main thread, then synchronously wait for its completion.
Calling it from main fails immediately instead of hanging the test process.
No application, native window, server, or installer is started by these tests.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

import nd2wsi.app as app


class MainQueue:
    def __init__(self, trace):
        self.owner = threading.current_thread()
        self.pending = queue.Queue()
        self.trace = trace
        self.native_callbacks = []
        self.hold_native = False
        self.dispatch_error = None

    def addOperationWithBlock_(self, operation):
        if self.dispatch_error is not None:
            raise self.dispatch_error
        self.native_callbacks.append(operation)
        if not self.hold_native:
            self.post(operation)

    def post(self, operation):
        self.pending.put(operation)

    def until(self, condition, timeout=3):
        assert threading.current_thread() is self.owner
        deadline = time.monotonic() + timeout
        while not condition():
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"Timed out; events: {self.trace}"
            try:
                operation = self.pending.get(timeout=min(remaining, 0.02))
            except queue.Empty:
                continue
            operation()


@dataclass
class Flush:
    script: str
    callback: object

    @property
    def request_id(self):
        argument = self.script.split("window.nd2wsiPrepareForUpdate(", 1)[1].split(")", 1)[0]
        return json.loads(argument)


class SemaphoreWebView:
    def __init__(self, main, trace):
        self.main = main
        self.trace = trace
        self.flushes = []
        self.calls = []
        self.main_calls = []
        self.waiters = []
        self.raise_on_flush = None
        self.cancellations = []
        self.recovery_requested = threading.Event()

    def evaluate_js(self, script, callback=None):
        caller = threading.current_thread()
        self.calls.append((script, caller))
        if caller is self.main.owner:
            self.main_calls.append(script)
            raise AssertionError("evaluate_js on main would deadlock Cocoa's semaphore")
        if callback is not None and self.raise_on_flush is not None:
            raise self.raise_on_flush
        finished = threading.Semaphore(0)
        self.waiters.append(finished)
        recovery = "window.nd2wsiCancelUpdate(" in script

        def execute_javascript():
            if recovery:
                self.trace.append("cancel-update")
                arguments = script.split("window.nd2wsiCancelUpdate(", 1)[1].rsplit(")", 1)[0]
                self.cancellations.append(json.loads(f"[{arguments}]"))
            elif callback is not None:
                self.trace.append("flush")
                self.flushes.append(Flush(script, callback))
            else:
                self.trace.append("notice")
            finished.release()

        self.main.post(execute_javascript)
        if recovery:
            self.recovery_requested.set()
        assert finished.acquire(timeout=3), "JavaScript callback was never delivered"
        return True

    def reply(self, index, result):
        # Deliver on main to ensure preparation also handles native callbacks
        # which arrive on a thread that cannot synchronously evaluate more JS.
        callback = self.flushes[index].callback
        self.main.post(lambda: callback(result))


class Api:
    def __init__(self, trace):
        self.trace = trace
        self.reason = None
        self.probed = threading.Event()
        self.closed = threading.Event()
        self.close_threads = []
        self.close_error = None
        self.close_entered = threading.Event()
        self.close_release = None

    def update_block_reason(self):
        self.trace.append("check-active-work")
        self.probed.set()
        return self.reason

    def stop_server_for_update(self):
        self.close_threads.append(threading.current_thread())
        self.trace.append("close-server")
        self.close_entered.set()
        if self.close_release is not None:
            assert self.close_release.wait(timeout=3), "Server teardown was never released"
        if self.close_error is not None:
            raise self.close_error
        self.closed.set()


@pytest.fixture
def driver(monkeypatch):
    trace = []
    main = MainQueue(trace)
    window = SemaphoreWebView(main, trace)
    api = Api(trace)
    logs, failures, completions = [], [], []
    monkeypatch.setattr(app, "_dlog", logs.append)
    monkeypatch.setitem(sys.modules, "Foundation", SimpleNamespace(
        NSOperationQueue=SimpleNamespace(mainQueue=lambda: main),
    ))
    coordinator = app.UpdateShutdownCoordinator(api, window)
    coordinator._FLUSH_TIMEOUT = 1.0
    coordinator._RETRY_DELAY = 0.02
    coordinator._DRAIN_POLL = 0.01
    initial_threads = set(threading.enumerate())

    def complete():
        trace.append("completion")
        completions.append(threading.current_thread())

    def prepare():
        return coordinator.prepare_for_update(complete, failures.append)

    def wait_workers():
        workers = [thread for thread in threading.enumerate()
                   if thread not in initial_threads and thread.name == "updater-safe-relaunch"]
        main.until(lambda: not any(thread.is_alive() for thread in workers))
        for thread in workers:
            thread.join(timeout=0.1)

    instance = SimpleNamespace(
        coordinator=coordinator, prepare=prepare, main=main, window=window,
        api=api, trace=trace, logs=logs, failures=failures, completions=completions,
        wait_workers=wait_workers,
    )
    yield instance
    coordinator.cancel_preparation()
    if api.close_release is not None:
        api.close_release.set()
    # Release a test waiter if an assertion interrupted main-queue pumping.
    for waiter in window.waiters:
        waiter.release()
    wait_workers()


def test_prepare_returns_without_blocking_main_and_relaunches_on_main(driver):
    assert driver.prepare() is True
    assert driver.window.main_calls == []
    assert driver.completions == []
    assert not driver.api.closed.is_set()
    driver.main.until(lambda: len(driver.window.flushes) == 1)
    assert "nd2wsiPrepareForUpdate" in driver.window.flushes[0].script
    driver.window.reply(0, {"ok": True, "panes": 2})
    driver.main.until(lambda: bool(driver.completions))
    assert driver.trace.index("flush") < driver.trace.index("close-server")
    assert driver.trace.index("close-server") < driver.trace.index("completion")
    assert driver.completions == [threading.main_thread()]
    assert all(thread is not threading.main_thread() for thread in driver.api.close_threads)
    assert all(thread is not threading.main_thread() for _, thread in driver.window.calls)
    assert driver.failures == []
    assert driver.coordinator._running is True


def test_active_exports_or_conversion_are_drained_before_server_close(driver):
    # Use the real API's conversion/export gating and shutdown order, replacing
    # only its HTTP server boundary so this regression opens no sockets.
    real_api = app.Api(None)
    exports = SimpleNamespace(count=2)
    shutdown_threads = []

    def shutdown():
        shutdown_threads.append(threading.current_thread())
        driver.trace.append("shutdown-server")

    real_api._frac = 0.5
    real_api._httpd = SimpleNamespace(
        registry=SimpleNamespace(active_export_count=lambda: exports.count),
        shutdown=shutdown, shutdown_for_relaunch=shutdown,
        server_close=lambda: driver.trace.append("server-close"),
    )
    driver.coordinator.api = real_api
    driver.prepare()
    driver.main.until(lambda: len(driver.window.flushes) == 1)
    driver.window.reply(0, {"ok": True, "panes": 1})
    driver.main.until(lambda: "notice" in driver.trace)
    assert shutdown_threads == []
    assert driver.completions == []
    real_api._frac = -1
    driver.main.until(lambda: driver.trace.count("notice") == 2)
    assert shutdown_threads == []
    assert real_api._httpd is not None
    exports.count = 0
    driver.main.until(lambda: bool(driver.completions))
    assert real_api._httpd is None
    assert len(shutdown_threads) == 1
    assert shutdown_threads[0] is not threading.main_thread()
    assert driver.trace.index("shutdown-server") < driver.trace.index("server-close")
    assert driver.trace.index("server-close") < driver.trace.index("completion")
    assert driver.window.main_calls == []


def test_duplicate_preparation_reports_failure_outside_the_coordinator_lock(driver):
    driver.prepare()
    driver.main.until(lambda: len(driver.window.flushes) == 1)
    results = []

    def duplicate_failure(message):
        acquired = driver.coordinator._lock.acquire(blocking=False)
        if acquired:
            driver.coordinator._lock.release()
        results.append({"message": message, "lock_free": acquired})

    assert driver.coordinator.prepare_for_update(lambda: None, duplicate_failure) is False
    driver.main.until(lambda: bool(results))
    assert results[0]["lock_free"] is True
    assert "already" in results[0]["message"].lower()
    driver.window.reply(0, {"ok": True})
    driver.main.until(lambda: bool(driver.completions))
    assert len(driver.window.flushes) == 1
    assert len(driver.api.close_threads) == 1
    assert driver.window.main_calls == []


@pytest.mark.parametrize("result", [
    {"ok": False, "error": "annotations could not be saved"}, None,
])
def test_failed_annotation_confirmation_retries_without_closing_the_server(driver, result):
    driver.prepare()
    driver.main.until(lambda: len(driver.window.flushes) == 1)
    driver.window.reply(0, result)
    driver.main.until(lambda: bool(driver.failures))
    assert not driver.api.probed.is_set()
    assert not driver.api.closed.is_set()
    assert driver.completions == []
    assert driver.window.main_calls == []
    driver.main.until(lambda: len(driver.window.flushes) == 2)
    assert driver.window.flushes[0].script != driver.window.flushes[1].script
    driver.window.reply(1, {"ok": True, "panes": 2})
    driver.main.until(lambda: bool(driver.completions))
    assert len(driver.failures) == 1
    assert len(driver.api.close_threads) == 1
    assert driver.completions == [threading.main_thread()]


def test_javascript_bridge_exception_reports_from_worker_and_can_retry(driver):
    driver.window.raise_on_flush = RuntimeError("bridge unavailable")
    driver.prepare()
    driver.main.until(lambda: bool(driver.failures))
    assert "bridge unavailable" in driver.failures[0]
    assert driver.window.main_calls == []
    assert not driver.api.closed.is_set()
    driver.window.raise_on_flush = None
    driver.main.until(lambda: len(driver.window.flushes) == 1)
    driver.window.reply(0, {"ok": True})
    driver.main.until(lambda: bool(driver.completions))
    assert driver.completions == [threading.main_thread()]


def test_timed_out_flush_reply_cannot_satisfy_a_later_failed_attempt(driver):
    driver.coordinator._FLUSH_TIMEOUT = 0.06
    driver.prepare()
    driver.main.until(lambda: len(driver.window.flushes) == 1)
    driver.main.until(lambda: len(driver.failures) == 1)
    driver.coordinator._FLUSH_TIMEOUT = 1.0
    driver.main.until(lambda: len(driver.window.flushes) == 2)
    assert driver.window.flushes[0].script != driver.window.flushes[1].script
    driver.window.reply(0, {"ok": True})  # old, timed-out promise
    driver.window.reply(1, {"ok": False, "error": "newer annotations are unsaved"})
    driver.main.until(lambda: len(driver.failures) == 2 or driver.api.closed.is_set())
    assert len(driver.failures) == 2
    assert "newer annotations are unsaved" in driver.failures[-1]
    assert not driver.api.closed.is_set()
    assert driver.completions == []
    driver.main.until(lambda: len(driver.window.flushes) == 3)
    driver.window.reply(2, {"ok": True})
    driver.main.until(lambda: bool(driver.completions))
    assert len(driver.api.close_threads) == 1


def test_cancel_invalidates_old_callbacks_and_allows_a_fresh_preparation(driver):
    driver.prepare()
    driver.main.until(lambda: len(driver.window.flushes) == 1)
    old_request = driver.coordinator._request_id
    old_wire_id = driver.window.flushes[0].request_id
    driver.coordinator.cancel_preparation()
    driver.wait_workers()
    assert driver.coordinator._running is False
    assert driver.window.cancellations == [[old_wire_id, False]]
    assert old_wire_id.startswith(f"{old_request}-")
    driver.prepare()
    assert driver.coordinator._request_id != old_request
    driver.main.until(lambda: len(driver.window.flushes) == 2)
    driver.window.reply(0, {"ok": True})
    driver.window.reply(1, {"ok": False, "error": "fresh preparation still unsaved"})
    driver.main.until(lambda: bool(driver.failures) or driver.api.closed.is_set())
    assert driver.failures and "fresh preparation still unsaved" in driver.failures[-1]
    assert not driver.api.closed.is_set()
    assert driver.coordinator._running is True
    driver.main.until(lambda: len(driver.window.flushes) == 3)
    driver.window.reply(2, {"ok": True})
    driver.main.until(lambda: bool(driver.completions))
    assert driver.completions == [threading.main_thread()]
    assert len(driver.api.close_threads) == 1
    assert driver.coordinator._running is True


def test_cancel_while_exports_are_active_never_closes_the_server(driver):
    driver.api.reason = "Waiting for an export"
    driver.prepare()
    driver.main.until(lambda: len(driver.window.flushes) == 1)
    driver.window.reply(0, {"ok": True})
    driver.main.until(lambda: "notice" in driver.trace)
    driver.coordinator.cancel_preparation()
    driver.api.reason = None
    driver.wait_workers()
    assert driver.coordinator._running is False
    assert not driver.api.closed.is_set()
    assert driver.completions == []
    assert driver.window.cancellations == [[driver.window.flushes[0].request_id, False]]


def test_cancel_during_teardown_keeps_ownership_until_browser_recovery_finishes(driver):
    driver.api.close_release = threading.Event()
    driver.prepare()
    driver.main.until(lambda: len(driver.window.flushes) == 1)
    old_request = driver.coordinator._request_id
    old_wire_id = driver.window.flushes[0].request_id
    driver.window.reply(0, {"ok": True})
    driver.main.until(driver.api.close_entered.is_set)
    driver.coordinator.cancel_preparation()
    assert driver.coordinator._running is True
    assert driver.coordinator._request_id == old_request
    assert driver.prepare() is False
    assert driver.failures == ["update preparation is already running"]
    assert len(driver.window.flushes) == 1
    assert driver.window.cancellations == []
    assert not driver.api.closed.is_set()

    driver.api.close_release.set()
    # Let the worker enqueue recovery without pumping the main queue. Its
    # synchronous browser call must retain ownership until that JS completes.
    assert driver.window.recovery_requested.wait(timeout=1)
    assert driver.api.closed.is_set()
    assert driver.window.cancellations == []
    assert driver.coordinator._running is True
    assert driver.coordinator._request_id == old_request
    assert driver.prepare() is False
    assert driver.failures == ["update preparation is already running"] * 2
    driver.wait_workers()
    assert driver.window.cancellations == [[old_wire_id, True]]
    assert driver.trace.index("close-server") < driver.trace.index("cancel-update")
    assert driver.coordinator._running is False
    assert driver.coordinator._request_id == ""
    assert driver.completions == []
    assert driver.main.native_callbacks == []
    assert driver.window.main_calls == []

    assert driver.prepare() is True
    assert driver.coordinator._request_id != old_request
    driver.main.until(lambda: len(driver.window.flushes) == 2)
    driver.window.reply(1, {"ok": True})
    driver.main.until(lambda: bool(driver.completions))
    assert driver.completions == [threading.main_thread()]
    assert len(driver.api.close_threads) == 2
    assert driver.coordinator._running is True


def test_cancelled_native_completion_cannot_resume_an_old_or_new_installer(driver):
    driver.main.hold_native = True
    driver.prepare()
    driver.main.until(lambda: len(driver.window.flushes) == 1)
    driver.window.reply(0, {"ok": True})
    driver.main.until(lambda: len(driver.main.native_callbacks) == 1)
    old_completion = driver.main.native_callbacks[0]
    driver.coordinator.cancel_preparation()
    driver.wait_workers()
    assert driver.window.cancellations == [[driver.window.flushes[0].request_id, True]]
    driver.prepare()
    driver.main.until(lambda: len(driver.window.flushes) == 2)
    old_completion()
    assert driver.completions == []
    assert driver.coordinator._running is True
    driver.window.reply(1, {"ok": True})
    driver.main.until(lambda: len(driver.main.native_callbacks) == 2)
    old_completion()
    assert driver.completions == []
    driver.main.native_callbacks[1]()
    assert driver.completions == [threading.main_thread()]
    assert driver.coordinator._running is True


def test_late_abort_after_native_completion_recovers_before_releasing_ownership(driver):
    assert driver.prepare() is True
    cancelled = driver.coordinator._cancelled
    real_wait = cancelled.wait
    waiting_after_completion = threading.Event()

    def observe_cancellation_wait(timeout=None):
        if driver.completions:
            waiting_after_completion.set()
        return real_wait(timeout)

    cancelled.wait = observe_cancellation_wait
    driver.main.until(lambda: len(driver.window.flushes) == 1)
    old_request = driver.coordinator._request_id
    old_wire_id = driver.window.flushes[0].request_id
    driver.window.reply(0, {"ok": True})
    driver.main.until(waiting_after_completion.is_set)
    assert driver.completions == [threading.main_thread()]
    assert driver.coordinator._running is True
    assert driver.coordinator._request_id == old_request
    assert driver.coordinator._wire_request_id == old_wire_id
    assert driver.window.cancellations == []
    assert driver.prepare() is False
    assert driver.failures == ["update preparation is already running"]

    # Sparkle's continuation returned successfully, but the installer can
    # still abort asynchronously before terminating this process.
    driver.coordinator.cancel_preparation()
    assert driver.window.recovery_requested.wait(timeout=1)
    assert driver.coordinator._running is True
    assert driver.coordinator._request_id == old_request
    assert driver.window.cancellations == []
    assert driver.prepare() is False
    driver.wait_workers()
    assert driver.window.cancellations == [[old_wire_id, True]]
    assert driver.coordinator._running is False
    assert driver.coordinator._request_id == ""
    assert driver.coordinator._wire_request_id == ""
    assert driver.window.main_calls == []
    driver.main.native_callbacks[0]()
    assert driver.completions == [threading.main_thread()]

    assert driver.prepare() is True
    driver.main.until(lambda: len(driver.window.flushes) == 2)
    driver.window.reply(1, {"ok": True})
    driver.main.until(lambda: len(driver.completions) == 2)
    assert driver.completions == [threading.main_thread()] * 2
    assert driver.coordinator._running is True


def test_worker_close_exception_reports_and_releases_the_request_for_retry(driver):
    driver.api.close_error = RuntimeError("source handle could not close")
    driver.prepare()
    driver.main.until(lambda: len(driver.window.flushes) == 1)
    driver.window.reply(0, {"ok": True})
    driver.main.until(lambda: bool(driver.failures) and not driver.coordinator._running)
    assert "source handle could not close" in driver.failures[-1]
    assert driver.completions == []
    assert driver.window.main_calls == []
    assert driver.window.cancellations == [[driver.window.flushes[0].request_id, True]]
    driver.api.close_error = None
    driver.prepare()
    driver.main.until(lambda: len(driver.window.flushes) == 2)
    driver.window.reply(1, {"ok": True})
    driver.main.until(lambda: bool(driver.completions))
    assert driver.completions == [threading.main_thread()]


def test_failure_to_dispatch_native_completion_never_calls_it_on_a_worker(driver):
    driver.main.dispatch_error = RuntimeError("main queue unavailable")
    driver.prepare()
    driver.main.until(lambda: len(driver.window.flushes) == 1)
    driver.window.reply(0, {"ok": True})
    driver.main.until(lambda: bool(driver.failures) and not driver.coordinator._running)
    assert "main queue unavailable" in driver.failures[-1]
    assert driver.completions == []
    assert driver.main.native_callbacks == []
    assert driver.window.main_calls == []
    assert driver.window.cancellations == [[driver.window.flushes[0].request_id, True]]


def test_native_completion_exception_recovers_on_worker_and_allows_retry(driver):
    completion_threads = []

    def failed_completion():
        completion_threads.append(threading.current_thread())
        raise RuntimeError("installer continuation unavailable")

    assert driver.coordinator.prepare_for_update(failed_completion, driver.failures.append) is True
    driver.main.until(lambda: len(driver.window.flushes) == 1)
    driver.window.reply(0, {"ok": True})
    driver.main.until(lambda: bool(driver.failures) and not driver.coordinator._running)
    assert completion_threads == [threading.main_thread()]
    assert "installer continuation unavailable" in driver.failures[-1]
    assert driver.window.cancellations == [[driver.window.flushes[0].request_id, True]]
    assert driver.window.main_calls == []
    assert driver.completions == []
    driver.prepare()
    driver.main.until(lambda: len(driver.window.flushes) == 2)
    driver.window.reply(1, {"ok": True})
    driver.main.until(lambda: bool(driver.completions))
    assert driver.completions == [threading.main_thread()]
    assert driver.coordinator._running is True


def test_failure_to_start_worker_does_not_leave_preparation_stuck(driver, monkeypatch):
    real_thread = threading.Thread

    class CannotStartThread(real_thread):
        def start(self):
            raise RuntimeError("worker resources unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(app.threading, "Thread", CannotStartThread)
        assert driver.prepare() is False
    assert driver.coordinator._running is False
    assert driver.failures and "worker resources unavailable" in driver.failures[-1]
    assert driver.window.calls == []
    assert driver.prepare() is True
    driver.main.until(lambda: len(driver.window.flushes) == 1)
    driver.window.reply(0, {"ok": True})
    driver.main.until(lambda: bool(driver.completions))
    assert driver.completions == [threading.main_thread()]
