"""One owner at a time for a window's panes: native close or Sparkle preparation.

Both coordinators drive the same shell, which remembers a single preparation
request id. Before they shared ownership, a close that started while Sparkle
was draining an export froze the panes under its own id, timed out, and
unlocked them, and the update then tore the server down under editable panes.

The fakes model the shell's side of the protocol (shell-v1.js) and the
window's API; a fake UI thread runs native closes and dispatched operations
one at a time, like AppKit's main queue.
"""

import json
import queue
import re
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nd2wsi import window_lifecycle

NOTICE = (
    "An update is waiting to install, so this window can't be closed now. "
    "The viewer will quit and reopen by itself when the update starts."
)
EXPORT_WAIT = "Waiting for 1 export to finish…"


def until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition was not reached"
        time.sleep(0.005)


class UiThread:
    """The native UI thread: runs posted operations one at a time, in order."""

    def __init__(self):
        self.errors = []
        self._queue = queue.Queue()
        self.thread = threading.Thread(target=self._run, name="fake-ui-thread", daemon=True)
        self.thread.start()

    def _run(self):
        while (operation := self._queue.get()) is not None:
            try:
                operation()
            except Exception as exc:
                self.errors.append(exc)

    def post(self, operation):
        self._queue.put(operation)

    def call(self, operation, timeout=5.0):
        """Run ``operation`` on the UI thread, as AppKit runs windowShouldClose:."""
        done = threading.Event()
        result = []

        def run():
            try:
                result.append(operation())
            finally:
                done.set()

        self.post(run)
        assert done.wait(timeout), "the UI thread did not run the operation"
        return result[0]

    def stop(self):
        self._queue.put(None)
        self.thread.join(5)


class Shell:
    """The shell's preparation protocol, as the window's only browser.

    Like ``nd2wsiPrepareForUpdate``/``nd2wsiCancelUpdate`` it remembers one
    current request id, and a cancellation unlocks the panes only while its
    id is still that one. One sent after the server's teardown keeps them
    frozen for good.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.current = ""
        self.frozen = False
        self.torn_down = False
        self.prepares = []
        self.cancels = []
        self.notices = []
        self.reply = {"ok": True, "panes": 1}
        self.hold_replies = False
        self.held = []
        self.notice_gate = threading.Event()
        self.notice_gate.set()
        self.destroyed = threading.Event()

    def evaluate_js(self, script, callback=None):
        notice = re.search(r'nd2wsiUpdateNotice\((".*")\)\s*$', script)
        if notice:
            self.notice_gate.wait(10)
            with self._lock:
                self.notices.append(json.loads(notice.group(1)))
            return None
        prepare = re.search(r'nd2wsiPrepareForUpdate\(("[^"]+")\)', script)
        cancel = re.search(r'nd2wsiCancelUpdate\(("[^"]+"), (true|false)\)', script)
        with self._lock:
            if prepare:
                wire_id = json.loads(prepare.group(1))
                self.current, self.frozen = wire_id, True
                self.prepares.append(wire_id)
                if self.hold_replies:
                    self.held.append(callback)
                    return None
                reply = dict(self.reply)
            elif cancel:
                wire_id = json.loads(cancel.group(1))
                honoured = wire_id == self.current
                if honoured:
                    self.torn_down = self.torn_down or cancel.group(2) == "true"
                    self.current, self.frozen = "", self.torn_down
                self.cancels.append((wire_id, honoured))
                return None
            else:
                raise AssertionError(f"unexpected script: {script}")
        if callback is not None:
            callback(reply)
        return None

    def answer(self, reply):
        with self._lock:
            callback = self.held.pop(0)
        callback(reply)

    def destroy(self):
        self.destroyed.set()


class Api:
    """The window's server state: one export can keep update and close waiting."""

    def __init__(self, shell):
        self.shell = shell
        self._httpd = object()
        self._frac = -1.0
        self._status = ""
        self.exports = 0
        self.frozen_at_teardown = None
        self.teardown_timeouts = []
        self.teardown_error = None

    def update_block_reason(self):
        return EXPORT_WAIT if self.exports else None

    def stop_server_for_update(self, timeout=None):
        self.teardown_timeouts.append(timeout)
        self.frozen_at_teardown = self.shell.frozen
        self._httpd = None
        if self.teardown_error is not None:
            raise self.teardown_error


@pytest.fixture()
def pair():
    ui = UiThread()
    shell = Shell()
    api = Api(shell)
    logs = []
    update = window_lifecycle.UpdateShutdownCoordinator(api, shell, logger=logs.append)
    close = window_lifecycle.WindowCloseCoordinator(api, shell, logger=logs.append)
    for coordinator in (update, close):
        coordinator._dispatch_to_ui = ui.post
        coordinator._FLUSH_TIMEOUT = 2.0
        coordinator._RETRY_DELAY = 0.02
        coordinator._DRAIN_POLL = 0.01
        coordinator._BROWSER_CALL_TIMEOUT = 1.0
    close._DRAIN_TIMEOUT = 0.2
    completions, failures = [], []
    state = SimpleNamespace(
        ui=ui, shell=shell, api=api, logs=logs, update=update, close=close,
        completions=completions, failures=failures,
        complete=lambda: completions.append(threading.current_thread()),
    )
    yield state
    shell.notice_gate.set()
    while shell.held:
        shell.answer({"ok": False, "error": "test finished"})
    update.cancel_preparation()
    close.cancel_preparation()
    until(lambda: not update._running and not close._running)
    ui.stop()
    assert ui.errors == []


def start_update_waiting_for_an_export(p):
    p.api.exports = 1
    prepared = len(p.shell.prepares)
    assert p.update.prepare_for_update(p.complete, p.failures.append) is True
    until(lambda: EXPORT_WAIT in p.shell.notices)
    assert p.shell.frozen and len(p.shell.prepares) == prepared + 1


def until_the_update_lets_go(p):
    # _running drops just before the worker releases ownership.
    until(lambda: not p.update._running and not p.close._ownership().held_by_other(p.close))


def test_close_while_an_update_is_prepared_is_vetoed_and_the_panes_stay_frozen(pair):
    p = pair
    start_update_waiting_for_an_export(p)

    assert p.ui.call(p.close.on_closing) is False
    assert p.close._running is False
    until(lambda: NOTICE in p.shell.notices)
    assert p.api._status == NOTICE
    # Long enough for a close request to time out on the export and unlock.
    time.sleep(p.close._DRAIN_TIMEOUT * 2)
    assert p.shell.frozen
    assert len(p.shell.prepares) == 1 and p.shell.cancels == []
    assert p.ui.call(p.close.on_closing) is False
    assert len(p.shell.prepares) == 1

    p.api.exports = 0
    until(lambda: p.completions)
    assert p.api.frozen_at_teardown is True
    assert p.completions == [p.ui.thread]
    assert p.shell.frozen and p.shell.cancels == []
    assert p.failures == []


def test_close_runs_its_save_handshake_once_the_cancelled_update_lets_go(pair):
    p = pair
    start_update_waiting_for_an_export(p)
    update_wire = p.shell.prepares[0]
    assert p.ui.call(p.close.on_closing) is False
    until(lambda: NOTICE in p.shell.notices)

    p.update.cancel_preparation()
    until_the_update_lets_go(p)
    assert p.shell.cancels == [(update_wire, True)]
    assert not p.shell.frozen
    until(lambda: p.api._status == "")

    p.api.exports = 0
    assert p.ui.call(p.close.on_closing) is False  # the handshake, not the veto
    assert p.shell.destroyed.wait(5)
    until(lambda: not p.close._running)
    assert p.close._approved is True
    assert len(p.shell.prepares) == 2
    assert p.shell.notices.count(NOTICE) == 1
    assert p.ui.call(p.close.on_closing) is True
    assert p.api.frozen_at_teardown is None


def test_idle_page_close_is_vetoed_while_an_update_is_prepared_and_passes_without_one(pair):
    p = pair
    # The page bridge never became ready and no slide is open, so a close has
    # nothing to save and normally passes at once without a request.
    p.shell.events = SimpleNamespace(_pywebviewready=threading.Event())
    p.api._httpd = SimpleNamespace(registry=SimpleNamespace(slides={}))
    p.api._frac = -1.0
    assert p.close._page_never_ready_without_slides() is True
    start_update_waiting_for_an_export(p)
    update_wire = p.shell.prepares[0]

    assert p.ui.call(p.close.on_closing) is False
    assert p.close._running is False
    until(lambda: NOTICE in p.shell.notices)
    assert p.api._status == NOTICE
    assert p.shell.frozen
    assert p.shell.prepares == [update_wire] and p.shell.cancels == []
    assert p.update._running is True
    assert p.ui.call(p.close.on_closing) is False
    assert p.close._running is False

    p.update.cancel_preparation()
    until_the_update_lets_go(p)
    assert p.shell.cancels == [(update_wire, True)]
    assert not p.shell.frozen

    assert p.ui.call(p.close.on_closing) is True
    assert p.close._running is False
    assert p.shell.prepares == [update_wire]
    assert any("page never became ready" in line for line in p.logs)
    # The idle close started no request, so it holds nothing: an update that
    # follows a close the window did not carry out still proceeds.
    assert not p.update._ownership().held_by_other(p.update)
    until(lambda: p.shell.notices.count(NOTICE) == 2)


def test_update_is_refused_while_a_close_is_saving_and_proceeds_after_it(pair):
    p = pair
    p.shell.hold_replies = True
    assert p.ui.call(p.close.on_closing) is False
    until(lambda: len(p.shell.held) == 1)
    close_wire = p.shell.prepares[0]

    refusals = []

    def refused(message):
        free = p.update._lock.acquire(blocking=False)
        if free:
            p.update._lock.release()
        refusals.append((message, free))

    assert p.update.prepare_for_update(p.complete, refused) is False
    assert len(refusals) == 1
    assert "closing" in refusals[0][0] and refusals[0][1] is True
    assert p.update._running is False
    assert p.shell.prepares == [close_wire]

    # The close is refused by the viewer, restores editing and lets go.
    p.shell.hold_replies = False
    p.shell.answer({"ok": False, "error": "annotation conflict: draft preserved"})
    until(lambda: not p.close._running)
    assert p.shell.cancels == [(close_wire, True)]
    assert "draft preserved" in p.api._status

    assert p.update.prepare_for_update(p.complete, p.failures.append) is True
    until(lambda: p.completions)
    assert p.api.frozen_at_teardown is True
    assert len(p.shell.prepares) == 2
    assert p.failures == []
    assert not p.shell.destroyed.is_set()


def test_forced_close_passes_while_an_update_is_prepared(pair):
    p = pair
    start_update_waiting_for_an_export(p)
    p.close.approve_close("GUI smoke failed in phase test")
    assert p.ui.call(p.close.on_closing) is True
    assert NOTICE not in p.shell.notices and p.api._status == ""
    assert p.update._running is True
    assert p.shell.frozen


def test_approved_close_still_passes_after_an_update_claims_the_window(pair):
    p = pair
    assert p.ui.call(p.close.on_closing) is False
    assert p.shell.destroyed.wait(5)
    until(lambda: not p.close._running)
    assert p.close._approved is True

    # The finished close let go, so the update proceeds; a repeated native
    # close (WinForms re-raises FormClosing) is still approved.
    start_update_waiting_for_an_export(p)
    assert p.ui.call(p.close.on_closing) is True
    assert NOTICE not in p.shell.notices


def test_sparkle_quit_after_the_installer_resumes_reaches_the_close_guard(pair):
    p = pair
    quits = []

    def resume_installer():
        # Sparkle answers the resumed install with a quit, which AppKit
        # delivers as a native close after this block returns.
        p.ui.post(lambda: quits.append(p.close.on_closing()))

    assert p.update.prepare_for_update(resume_installer, p.failures.append) is True
    until(lambda: quits)
    assert quits == [False]  # the save handshake runs, as before
    assert p.shell.destroyed.wait(5)
    until(lambda: not p.close._running)
    assert p.close._approved is True
    assert NOTICE not in p.shell.notices
    assert p.api.frozen_at_teardown is True
    assert p.update._running is True  # still waiting for Sparkle to end the process


def test_close_after_sparkle_aborts_past_teardown_needs_no_save_handshake(pair):
    p = pair
    resumed = threading.Event()
    assert p.update.prepare_for_update(resumed.set, p.failures.append) is True
    until(resumed.is_set)
    update_wire = p.shell.prepares[0]
    assert p.api.frozen_at_teardown is True and p.api._httpd is None

    # Sparkle aborts after the server stopped: the shell keeps the panes
    # frozen, since nothing edited now could be saved.
    p.update.cancel_preparation()
    until_the_update_lets_go(p)
    assert p.shell.cancels == [(update_wire, True)]
    assert p.shell.frozen

    # So the next close asks the panes nothing and the window closes at once.
    assert p.ui.call(p.close.on_closing) is True
    assert p.close._running is False
    assert p.shell.prepares == [update_wire]
    assert any("an update already saved the panes and stopped the server" in line for line in p.logs)
    assert p.failures == []

    # A slide opened since would have started a new server with new edits.
    p.api._httpd = object()
    assert p.ui.call(p.close.on_closing) is False
    assert p.shell.destroyed.wait(5)
    until(lambda: not p.close._running)
    assert len(p.shell.prepares) == 2


def test_a_teardown_that_times_out_is_reported_and_the_window_still_closes(pair):
    p = pair
    p.update._TEARDOWN_TIMEOUT = 0.05
    p.update._TEARDOWN_LIMIT = 0.2
    p.api.teardown_error = TimeoutError("a slide read did not finish within 0.05 seconds")
    assert p.update.prepare_for_update(p.complete, p.failures.append) is True
    until_the_update_lets_go(p)
    message = ("Update could not close the viewer safely: a slide read did not finish within 0.2 seconds. "
               "Quit and reopen the viewer to install the update.")
    assert p.failures == [message]
    assert p.shell.notices.count("Waiting for a slide read to finish before installing the update…") == 1
    assert message in p.shell.notices
    assert p.completions == []
    assert p.api.teardown_timeouts[0] == 0.05
    assert all(timeout <= 0.2 for timeout in p.api.teardown_timeouts)
    assert p.shell.cancels == [(p.shell.prepares[0], True)] and p.shell.frozen

    assert p.ui.call(p.close.on_closing) is True
    assert p.shell.prepares[1:] == []


def test_the_close_veto_notice_clears_when_sparkle_aborts(pair):
    p = pair
    # The start page polls the status; after an abort it must not keep saying
    # an update is about to install.
    p.shell.events = SimpleNamespace(_pywebviewready=threading.Event())
    p.api._httpd = SimpleNamespace(registry=SimpleNamespace(slides={}))
    start_update_waiting_for_an_export(p)
    assert p.ui.call(p.close.on_closing) is False
    assert p.api._status == NOTICE

    p.update.cancel_preparation()
    until_the_update_lets_go(p)
    until(lambda: p.api._status == "")

    # A veto that found the preparation still owning the panes, but sets its
    # notice only after the preparation let go, does not leave it behind.
    p.close._refuse_close_while_preparing_update()
    assert p.api._status == ""


def test_the_close_veto_notice_clear_keeps_any_other_status(pair):
    p = pair
    start_update_waiting_for_an_export(p)
    assert p.ui.call(p.close.on_closing) is False
    p.api._status = "could not open slide.nd2: file is missing"
    p.update.cancel_preparation()
    until(lambda: not any(thread.name == "updater-safe-relaunch" and thread.is_alive()
                          for thread in threading.enumerate()))
    assert p.api._status == "could not open slide.nd2: file is missing"


def test_veto_does_not_wait_for_a_viewer_that_cannot_show_the_notice(pair):
    p = pair
    start_update_waiting_for_an_export(p)
    p.close._BROWSER_CALL_TIMEOUT = 3.0
    p.shell.notice_gate.clear()
    started = time.monotonic()
    assert p.ui.call(p.close.on_closing) is False
    assert time.monotonic() - started < 1.5
    p.shell.notice_gate.set()
    until(lambda: NOTICE in p.shell.notices)


def test_ownership_is_one_record_per_api_and_released_only_by_its_holder():
    shell = Shell()
    api = SimpleNamespace(_httpd=None, _frac=-1.0, _status="")
    update = window_lifecycle.UpdateShutdownCoordinator(api, shell, logger=Mock())
    close = window_lifecycle.WindowCloseCoordinator(api, shell, logger=Mock())
    ownership = update._ownership()
    assert close._ownership() is ownership is api._shutdown_ownership

    assert ownership.claim(update, "update-1")
    assert not ownership.claim(close, "close-1")
    assert ownership.held_by_other(close) and not ownership.held_by_other(update)
    ownership.release(close, "update-1")
    ownership.release(update, "stale")
    assert ownership.held_by_other(close)
    ownership.release(update, "update-1")
    assert ownership.claim(close, "close-1")

    other = window_lifecycle.UpdateShutdownCoordinator(SimpleNamespace(), shell, logger=Mock())
    assert other._ownership() is not ownership
    # A test double that answers every attribute still gets a real record.
    mocked = window_lifecycle.UpdateShutdownCoordinator(Mock(), shell, logger=Mock())
    assert isinstance(mocked._ownership(), window_lifecycle._ShutdownOwnership)
