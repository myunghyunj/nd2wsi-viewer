"""Delegate request ownership without Cocoa, browser, server, or timer threads."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

import nd2wsi.updater as updater


class NSObject:
    def __init_subclass__(cls, protocols=(), **kwargs):
        super().__init_subclass__(**kwargs)

    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self


class Coordinator:
    def __init__(self):
        self.accept = True
        self.calls = []
        self.cancellations = 0
        self.rejection = "update preparation is already running"

    def prepare_for_update(self, complete, fail):
        self.calls.append(SimpleNamespace(complete=complete, fail=fail, accepted=self.accept))
        if not self.accept:
            fail(self.rejection)
        return self.accept

    def cancel_preparation(self):
        self.cancellations += 1


@pytest.fixture
def driver(monkeypatch):
    timers = []

    class Timer:
        def __init__(self, interval, callback):
            self.interval = interval
            self.callback = callback
            self.daemon = False
            self.cancelled = False

        def start(self):
            timers.append(self)

        def cancel(self):
            self.cancelled = True

        def fire(self):
            # Also allow a cancelled, already-queued callback to run.
            self.callback()

    monkeypatch.setitem(sys.modules, "objc", SimpleNamespace(super=super))
    monkeypatch.setitem(sys.modules, "Foundation", SimpleNamespace(NSObject=NSObject))
    monkeypatch.setattr(updater, "_DELEGATE_TYPE", None)
    monkeypatch.setattr(updater.threading, "Timer", Timer)
    coordinator, logs = Coordinator(), []
    delegate = updater._delegate_class(object()).alloc().initWithCoordinator_logger_(
        coordinator, logs.append,
    )
    yield SimpleNamespace(
        coordinator=coordinator, delegate=delegate, timers=timers, logs=logs,
        prepare=lambda handler: delegate.updater_shouldPostponeRelaunchForUpdate_untilInvokingBlock_(
            None, None, handler,
        ),
        abort=lambda: delegate.updater_didAbortWithError_(None, "cancelled"),
    )


def test_duplicate_request_keeps_original_block_and_completes_once(driver):
    installed = []

    def first():
        installed.append("first")

    assert driver.prepare(first) is True
    assert driver.prepare(lambda: installed.append("duplicate")) is True
    assert driver.delegate._pending_install_handler is first
    assert len(driver.coordinator.calls) == 1
    driver.coordinator.calls[0].complete()
    driver.coordinator.calls[0].complete()
    assert installed == ["first"]
    assert driver.delegate._pending_install_handler is None
    assert driver.timers == []


def test_aborted_completion_cannot_invoke_or_clear_a_new_request(driver):
    installed = []
    driver.prepare(lambda: installed.append("aborted"))
    old = driver.coordinator.calls[0]
    driver.abort()

    def second():
        installed.append("second")

    driver.prepare(second)
    old.complete()
    old.fail("stale failure")
    assert installed == []
    assert driver.delegate._pending_install_handler is second
    assert not any("stale failure" in message for message in driver.logs)
    driver.coordinator.calls[-1].complete()
    assert installed == ["second"]
    assert driver.coordinator.cancellations == 1


def test_new_request_waits_for_aborted_worker_recovery(driver):
    installed = []
    driver.prepare(lambda: installed.append("aborted"))
    old = driver.coordinator.calls[0]
    driver.abort()
    driver.coordinator.accept = False

    def second():
        installed.append("second")

    driver.prepare(second)
    assert len(driver.timers) == 1
    assert driver.timers[0].daemon is True
    assert 0 < driver.timers[0].interval < 1
    old.complete()
    driver.timers[-1].fire()
    assert len(driver.timers) == 2
    assert installed == []
    assert driver.delegate._pending_install_handler is second
    assert not any("already running" in message for message in driver.logs)
    driver.coordinator.accept = True
    driver.timers[-1].fire()
    assert driver.coordinator.calls[-1].accepted is True
    assert driver.delegate._pending_install_timer is None
    assert installed == []  # Timer admission never invokes the native block.
    driver.coordinator.calls[-1].complete()
    assert installed == ["second"]


def test_abort_cancels_retry_and_queued_stale_retry_cannot_replace_new_timer(driver):
    driver.coordinator.accept = False
    driver.prepare(lambda: None)
    old_timer = driver.timers[-1]
    old_call = driver.coordinator.calls[-1]
    driver.abort()
    assert old_timer.cancelled is True
    driver.prepare(lambda: None)
    new_timer = driver.timers[-1]
    calls = len(driver.coordinator.calls)
    old_timer.fire()
    old_call.complete()
    assert len(driver.coordinator.calls) == calls
    assert driver.delegate._pending_install_timer is new_timer
    assert new_timer.cancelled is False


def test_completion_can_reenter_with_new_request_without_losing_its_handler(driver):
    installed = []

    def second():
        installed.append("second")

    def first():
        installed.append("first")
        driver.coordinator.accept = False
        driver.prepare(second)

    driver.prepare(first)
    original = driver.coordinator.calls[0]
    original.complete()
    assert installed == ["first"]
    assert driver.delegate._pending_install_handler is second
    original.complete()
    assert installed == ["first"]
    driver.coordinator.accept = True
    driver.timers[-1].fire()
    driver.coordinator.calls[-1].complete()
    assert installed == ["first", "second"]


def test_repeated_start_failure_is_retried_without_log_spam(driver):
    driver.coordinator.accept = False
    driver.coordinator.rejection = "Could not start update preparation: no worker available"
    driver.prepare(lambda: None)
    driver.timers[-1].fire()
    driver.timers[-1].fire()
    assert len(driver.coordinator.calls) == 3
    assert driver.logs == [
        "Sparkle relaunch postponed: Could not start update preparation: no worker available",
    ]
    driver.abort()
    assert driver.timers[-1].cancelled is True


def test_abort_after_native_completion_still_cancels_coordinator(driver):
    installed = []
    driver.prepare(lambda: installed.append("done"))
    driver.coordinator.calls[0].complete()
    driver.abort()
    assert installed == ["done"]
    assert driver.coordinator.cancellations == 1
