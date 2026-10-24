"""Save-before-close on the WinForms UI thread, and the GUI smoke's close path.

The coordinator is driven end to end against a fake pywebview WinForms window
(see ``winforms_fake``) and a fake AppKit window whose ``destroy()`` is
``NSWindow.close`` and therefore never asks ``windowShouldClose:``.
"""

import queue
import re
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from winforms_fake import FAKE_SYSTEM, Action, Event, FakeWinFormsWindow

from nd2wsi import app, window_lifecycle

BOOT = {"bridge": True, "version": "test", "boot": True}


def until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition was not reached"
        time.sleep(0.005)


def wire_id(script, function):
    return re.search(function + r'\("([^"]+)"', script).group(1)


def make_api():
    return SimpleNamespace(_httpd=object(), _frac=-1, _status="",
                           update_block_reason=Mock(return_value=None))


@pytest.fixture()
def win32(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "System", FAKE_SYSTEM)
    # Any Cocoa import on Windows fails, and the close would then stay vetoed.
    monkeypatch.setitem(sys.modules, "Foundation", None)
    window = FakeWinFormsWindow()
    yield window
    window.native.exit_loop()
    assert window.native.errors == []


@pytest.fixture()
def winforms(win32):
    window = win32
    api = make_api()
    logs = []
    guard = window_lifecycle.WindowCloseCoordinator(api, window, logger=logs.append)
    guard._FLUSH_TIMEOUT = 2.0
    guard._DRAIN_TIMEOUT = 0.1
    guard._DRAIN_POLL = 0.005
    window.events.closing += guard.on_closing
    window.events.closed += guard.cancel_preparation
    yield SimpleNamespace(window=window, api=api, guard=guard, logs=logs)
    guard.cancel_preparation()


def test_title_bar_close_is_vetoed_saved_then_closed_once_from_the_ui_thread(winforms):
    w = winforms
    w.window.user_close()
    until(lambda: w.window.is_closed and not w.guard._running)
    # FormClosing was cancelled once, then the approved Form.Close went through.
    assert w.window.close_results == ["cancelled", "closed"]
    assert w.window.closed_count == 1
    assert w.guard._approved is True
    assert "nd2wsiPrepareForUpdate" in w.window.scripts[0]
    assert not any("nd2wsiCancelUpdate" in script for script in w.window.scripts)
    # The worker handed approval back with BeginInvoke(Action(...)); destroy ran on the UI thread.
    assert len(w.window.native.begin_invokes) == 1
    assert w.window.destroy_threads == [w.window.native.thread]
    # closed -> cancel_preparation retires the finished request.
    until(lambda: w.guard._cancelled.is_set())
    assert w.window.native.loop_ended.wait(1)
    assert w.guard.on_closing() is True


@pytest.mark.parametrize("failure", ["save", "timeout", "export", "destroy"])
def test_unconfirmed_close_keeps_the_window_open_and_restores_editing(winforms, failure):
    w = winforms
    if failure == "save":
        w.window.flush_reply = {"ok": False, "error": "annotation conflict: draft preserved"}
    elif failure == "timeout":
        w.guard._FLUSH_TIMEOUT = 0.05
        w.window.auto_reply = False
    elif failure == "export":
        w.api.update_block_reason.return_value = "Waiting for 1 export to finish…"
    elif failure == "destroy":
        w.window.destroy.side_effect = RuntimeError("native close failed")
    w.window.user_close()
    until(lambda: not w.guard._running)
    assert not w.window.is_closed and w.window.close_results == ["cancelled"]
    assert w.guard._approved is False
    assert "Window remains open" in w.api._status
    assert any("nd2wsiCancelUpdate" in script for script in w.window.scripts)
    assert any("nd2wsiUpdateNotice" in script for script in w.window.scripts)
    if failure == "save":
        assert "draft preserved" in w.api._status
    if failure != "destroy":
        w.window.destroy.assert_not_called()
        # The user can close again once the work is saved or finished.
        w.window.flush_reply = {"ok": True, "panes": 1}
        w.window.auto_reply = True
        w.api.update_block_reason.return_value = None
        w.window.user_close()
        until(lambda: w.window.is_closed)
        assert w.window.close_results == ["cancelled", "cancelled", "closed"]


def test_second_close_while_saving_is_refused_without_a_second_request(winforms):
    w = winforms
    w.window.auto_reply = False
    w.window.user_close()
    until(lambda: w.window.pending_callbacks)
    w.window.user_close()  # Alt+F4 again while annotations are still saving
    assert w.window.close_results == ["cancelled", "cancelled"]
    assert len(w.window.pending_callbacks) == 1
    w.window.reply(w.window.pending_callbacks[0], {"ok": True, "panes": 2})
    until(lambda: w.window.is_closed)
    assert w.window.close_results == ["cancelled", "cancelled", "closed"]
    assert w.window.closed_count == 1


def test_closed_event_cancels_a_running_request_and_a_late_reply_cannot_close(winforms):
    w = winforms
    w.window.auto_reply = False
    w.window.user_close()
    until(lambda: w.window.pending_callbacks)
    w.window.events.closed.set()  # the Form was torn down underneath the request
    until(lambda: not w.guard._running)
    assert w.guard._cancelled.is_set() and w.guard._approved is False
    w.window.pending_callbacks[0]({"ok": True, "panes": 1})
    time.sleep(0.05)
    assert w.window.native.begin_invokes == []
    w.window.destroy.assert_not_called()


def test_win32_dispatch_begin_invokes_off_the_ui_thread_and_runs_inline_on_it(winforms):
    w = winforms
    native = w.window.native
    ran = []
    w.guard._dispatch_to_ui(lambda: ran.append(threading.current_thread()))
    until(lambda: ran)
    assert ran == [native.thread] and len(native.begin_invokes) == 1

    def on_ui_thread():  # InvokeRequired is False here
        w.guard._dispatch_to_ui(lambda: ran.append("inline"))
        ran.append("after")

    native.Invoke(on_ui_thread)
    assert ran[1:] == ["inline", "after"]
    assert len(native.begin_invokes) == 1


def test_win32_dispatch_never_raises_into_the_message_loop(winforms):
    w = winforms
    w.guard._dispatch_to_ui(lambda: 1 / 0)
    until(lambda: w.logs)
    assert "ZeroDivisionError" in w.logs[-1]
    assert w.window.native.errors == []


def test_dispatch_refuses_a_platform_without_a_native_ui_thread(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    guard = window_lifecycle.WindowCloseCoordinator(make_api(), SimpleNamespace(), logger=Mock())
    with pytest.raises(RuntimeError, match="No native UI thread"):
        guard._dispatch_to_ui(Mock())


def test_approve_close_cancels_the_request_and_lets_the_next_close_through(winforms):
    w = winforms
    w.window.auto_reply = False
    w.window.user_close()
    until(lambda: w.window.pending_callbacks)
    w.guard.approve_close("diagnostic")
    until(lambda: not w.guard._running)
    # The closing browser is left alone rather than unlocked mid-teardown.
    assert not any("nd2wsiCancelUpdate" in script for script in w.window.scripts)
    w.window.destroy()
    assert w.window.close_results == ["cancelled", "closed"]
    assert "diagnostic" in w.logs[-1]


def test_cancel_after_dispatch_never_approves_because_finish_decides_on_the_ui_thread(winforms):
    w = winforms
    native = w.window.native
    w.window.auto_reply = False
    w.window.user_close()
    until(lambda: w.window.pending_callbacks)
    gate = threading.Event()
    native.BeginInvoke(Action(lambda: gate.wait(5)))  # occupy the UI thread
    w.window.reply(w.window.pending_callbacks[0], {"ok": True, "panes": 1})
    until(lambda: len(native.begin_invokes) == 2)  # finish() is queued behind the gate
    # Saved state is confirmed, yet only finish() on the UI thread may approve.
    assert w.guard._approved is False
    w.guard.cancel_preparation()  # e.g. the Form was torn down underneath the request
    gate.set()
    drained = threading.Event()
    native.BeginInvoke(Action(drained.set))  # FIFO: the queued finish() has run
    assert drained.wait(2)
    until(lambda: not w.guard._running)
    assert w.guard._approved is False and not w.window.is_closed
    w.window.destroy.assert_not_called()
    assert any("nd2wsiCancelUpdate" in script for script in w.window.scripts)
    w.window.auto_reply = True
    w.window.user_close()  # still vetoed: a fresh handshake starts, then closes
    until(lambda: w.window.is_closed)
    assert w.window.close_results == ["cancelled", "cancelled", "closed"]


@pytest.mark.parametrize("answer", [True, False])
def test_hung_viewer_cannot_wedge_close_and_a_repeated_close_offers_to_force_it(winforms, answer):
    w = winforms
    w.guard._FLUSH_TIMEOUT = 0.2
    w.guard._BROWSER_CALL_TIMEOUT = 0.05
    released = threading.Event()

    def hung(script, callback=None):
        # WinForms evaluate_js waits on a semaphore for a continuation that a
        # hung renderer never runs; nothing here ever calls back.
        assert w.window.native.InvokeRequired
        w.window.scripts.append(script)
        released.wait(10)

    w.window.evaluate_js.side_effect = hung
    try:
        w.window.user_close()
        w.window.user_close()  # refused while the first handshake runs
        until(lambda: not w.guard._running, timeout=3)
        assert w.api._status == "Window remains open: the viewer did not confirm its saved state"
        assert any(line.startswith("update notice failed: the viewer did not answer") for line in w.logs)
        assert any(line.startswith("update cancellation recovery failed: the viewer did not answer")
                   for line in w.logs)
        # Nothing more reaches the stuck viewer; its recovery waits behind it.
        assert len(w.window.scripts) == 1 and "nd2wsiPrepareForUpdate" in w.window.scripts[0]
        w.window.create_confirmation_dialog.assert_not_called()  # a first close never asks
        w.window.dialog_answer = answer
        w.window.user_close()  # the user closes again
        until(lambda: w.window.create_confirmation_dialog.called, timeout=3)
        until(lambda: not w.guard._running, timeout=3)
        title, message = w.window.create_confirmation_dialog.call_args.args
        assert title == "Close without confirmed saves?"
        assert "did not confirm its saved state" in message
        # Asked from the close worker; the UI thread is never blocked by it.
        assert w.window.dialog_threads[0] is not w.window.native.thread
        assert w.window.dialog_threads[0].name == "window-safe-close"
        if answer:
            assert w.window.is_closed and w.guard._approved is True
            assert w.window.close_results == ["cancelled", "cancelled", "cancelled", "closed"]
            assert w.window.destroy_threads == [w.window.native.thread]
        else:
            assert not w.window.is_closed and w.guard._approved is False
            assert w.window.close_results == ["cancelled", "cancelled", "cancelled"]
            assert w.api._status == "Window remains open: the viewer did not confirm its saved state"
            w.window.destroy.assert_not_called()
        assert len(w.window.scripts) == 1  # the repeated handshake sent nothing either
    finally:
        released.set()
    # The viewer recovers: the queued recovery runs after the preparation it undoes.
    until(lambda: len(w.window.scripts) == 2)
    prepare, cancel = w.window.scripts
    assert wire_id(cancel, "nd2wsiCancelUpdate") == wire_id(prepare, "nd2wsiPrepareForUpdate")


@pytest.mark.parametrize("state,closes_at_once", [
    ("no-server", True), ("empty-registry", True),
    ("slide-open", False), ("slide-opening", False), ("unknown-registry", False), ("bridge-ready", False),
])
def test_only_a_never_ready_page_without_slides_closes_without_a_handshake(winforms, state, closes_at_once):
    w = winforms
    w.window.events._pywebviewready.clear()  # e.g. WebView2 failed to initialise
    w.api._httpd = {
        "no-server": None,
        "empty-registry": SimpleNamespace(registry=SimpleNamespace(slides={})),
        "slide-open": SimpleNamespace(registry=SimpleNamespace(slides={"s1": object()})),
        "slide-opening": None,
        "unknown-registry": object(),
        "bridge-ready": None,
    }[state]
    if state == "slide-opening":
        w.api._frac = 0.0
        w.api.update_block_reason.return_value = None
    if state == "bridge-ready":
        w.window.events._pywebviewready.set()
    w.window.user_close()
    if closes_at_once:
        assert w.window.close_results == ["closed"] and w.window.is_closed
        w.window.evaluate_js.assert_not_called()
        assert w.guard._running is False and w.guard._approved is False
        assert "the page never became ready and no slide is open" in w.logs[-1]
    else:
        assert w.window.close_results[0] == "cancelled"
        until(lambda: w.window.is_closed and not w.guard._running)
        assert "nd2wsiPrepareForUpdate" in w.window.scripts[0]


def test_never_ready_page_with_a_slide_fails_then_a_repeated_close_can_force_it(winforms):
    w = winforms
    w.window.events._pywebviewready.clear()
    w.api._httpd = SimpleNamespace(registry=SimpleNamespace(slides={"s1": object()}))

    def never_ready(script, callback=None):
        w.window.scripts.append(script)
        time.sleep(0.02)  # pywebview first waits 20 s for the page bridge
        raise RuntimeError("Main window failed to start")

    w.window.evaluate_js.side_effect = never_ready
    w.window.user_close()
    until(lambda: not w.guard._running)
    assert w.api._status == "Window remains open: Main window failed to start"
    assert not w.window.is_closed
    w.window.create_confirmation_dialog.assert_not_called()
    w.window.dialog_answer = True
    w.window.user_close()
    until(lambda: w.window.is_closed and not w.guard._running)
    assert w.window.close_results == ["cancelled", "cancelled", "closed"]
    assert "Main window failed to start" in w.window.create_confirmation_dialog.call_args.args[1]
    assert any("Closing without confirmed saves at the user's request" in line for line in w.logs)


def test_repeated_close_succeeds_without_asking_once_the_viewer_confirms(winforms):
    w = winforms
    w.window.flush_reply = {"ok": False, "error": "annotation conflict: draft preserved"}
    w.window.user_close()
    until(lambda: not w.guard._running)
    w.window.flush_reply = {"ok": True, "panes": 1}  # the user resolved the conflict
    w.window.dialog_answer = True
    w.window.user_close()
    until(lambda: w.window.is_closed and not w.guard._running)
    w.window.create_confirmation_dialog.assert_not_called()
    assert w.guard._failed_closes == 0


def test_update_preparation_retries_a_hung_viewer_without_piling_up_calls_and_recovers_it():
    released = threading.Event()
    scripts = []

    def hung(script, callback=None):
        scripts.append(script)
        released.wait(10)

    window = SimpleNamespace(evaluate_js=Mock(side_effect=hung))
    logs, failures, completion = [], [], Mock()
    coordinator = window_lifecycle.UpdateShutdownCoordinator(make_api(), window, logger=logs.append)
    coordinator._FLUSH_TIMEOUT = 0.05
    coordinator._BROWSER_CALL_TIMEOUT = 0.02
    coordinator._RETRY_DELAY = 0.01
    before = set(threading.enumerate())
    try:
        assert coordinator.prepare_for_update(completion, failures.append) is True
        until(lambda: len(failures) >= 3, timeout=5)  # Sparkle keeps retrying
        assert all("the viewer did not confirm its saved state" in failure for failure in failures)
        # Only the first preparation reached the stuck viewer: one stranded call, not one per retry.
        assert len(scripts) == 1
        calls = [thread for thread in threading.enumerate()
                 if thread not in before and thread.name == "window-evaluate-js"]
        assert len(calls) == 1
        coordinator.cancel_preparation()
        # Recovery cannot run in a hung viewer, yet ownership is released.
        until(lambda: not coordinator._running, timeout=3)
        assert any(line.startswith("update cancellation recovery failed") for line in logs)
        completion.assert_not_called()
    finally:
        released.set()
    # Once the viewer answers, the queued recovery unlocks the preparation it was sent.
    until(lambda: len(scripts) == 2)
    assert wire_id(scripts[1], "nd2wsiCancelUpdate") == wire_id(scripts[0], "nd2wsiPrepareForUpdate")


# GUI smoke -------------------------------------------------------------------

@pytest.fixture()
def smoke(monkeypatch, tmp_path, win32):
    """The real app window wiring on the fake WinForms backend."""
    window = win32
    webview = SimpleNamespace(create_window=Mock(return_value=window), settings={},
                              renderer="edgechromium")
    monkeypatch.setitem(sys.modules, "webview", webview)
    monkeypatch.setenv("ND2WSI_WINDOW_SESSION_ROOT", str(tmp_path / "sessions"))
    monkeypatch.delenv("ND2WSI_SMOKE_LUT_CLIPBOARD", raising=False)
    monkeypatch.setattr(app, "_wire_file_drop", Mock())
    monkeypatch.setattr(app, "_dlog", Mock())
    monkeypatch.setattr("nd2wsi.translucent_smoke.exercise_translucent_viewer", Mock(return_value={"ok": True}))
    monkeypatch.setattr("nd2wsi.window_smoke.exercise_independent_window", Mock(return_value={"ok": True}))
    api, _ = app.create_app_window(None)
    api._close_coordinator._FLUSH_TIMEOUT = 2.0
    return SimpleNamespace(api=api, window=window)


@pytest.mark.parametrize("failure", ["lut", "never-ready"])
def test_failed_gui_smoke_force_approves_and_closes_without_waiting(smoke, failure):
    window = smoke.window
    if failure == "lut":
        window.evaluate_js.return_value = {"bridge": True, "version": "test", "slide": True,
                                           "lut_toolbar": {"ok": False, "error": "LUT icon missing"}}
    started = time.monotonic()
    report = app.gui_smoke(smoke.api, window, expect_slide=True, timeout=0 if failure == "never-ready" else 90)
    assert time.monotonic() - started < 2
    assert report["ok"] is False
    assert report["close"]["path"] == "forced"
    assert window.is_closed and window.close_results == ["closed"]
    assert not any("nd2wsiPrepareForUpdate" in script for script in window.scripts)


def test_successful_gui_smoke_records_the_coordinated_close_before_the_loop_returns(smoke):
    window = smoke.window
    window.evaluate_js.return_value = BOOT
    # pywebview runs the smoke on its own thread; main() writes the report as
    # soon as the GUI loop ends, so the outcome must already be final then.
    thread = threading.Thread(target=app.gui_smoke, args=(smoke.api, window), daemon=True)
    thread.start()
    assert window.native.loop_ended.wait(5)
    final = dict(smoke.api._gui_smoke_report)
    assert final["ok"] is True and final["phase"] == "complete"
    assert final["close"]["path"] == "coordinated" and final["close"]["seconds"] >= 0
    assert "error" not in final
    assert window.close_results == ["cancelled", "closed"]
    assert window.destroy_threads[-1] is window.native.thread
    assert any("nd2wsiPrepareForUpdate" in script for script in window.scripts)
    thread.join(5)
    assert not thread.is_alive()


def test_coordinated_close_that_never_completes_is_forced_and_fails_the_smoke(smoke):
    window = smoke.window
    window.evaluate_js.return_value = BOOT
    window.auto_reply = False  # the viewer never confirms its saved state
    report = app.gui_smoke(smoke.api, window, close_timeout=0.1)
    assert report["ok"] is False and report["phase"] == "close"
    assert "did not complete the save handshake within 0.1 seconds" in report["error"]
    assert report["close"]["path"] == "forced"
    assert window.is_closed and window.close_results == ["cancelled", "closed"]
    until(lambda: not smoke.api._close_coordinator._running)
    # The smoke closes once, so the native force-close question never appears.
    window.create_confirmation_dialog.assert_not_called()


# macOS -----------------------------------------------------------------------

class FakeAppKit:
    """AppKit's main queue plus an NSWindow whose close skips windowShouldClose:."""

    def __init__(self):
        self.queue = queue.Queue()
        self.thread = threading.Thread(target=self._loop, name="fake-appkit-main", daemon=True)
        self.loop_ended = threading.Event()
        self.close_results = []
        self.thread.start()

    def addOperationWithBlock_(self, block):  # noqa: N802 - AppKit selector
        self.queue.put(block)

    def _loop(self):
        while (block := self.queue.get()) is not None:
            block()
        self.loop_ended.set()


def test_macos_smoke_close_uses_perform_close_and_the_same_handshake(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    appkit = FakeAppKit()
    monkeypatch.setitem(sys.modules, "Foundation", SimpleNamespace(
        NSOperationQueue=SimpleNamespace(mainQueue=lambda: appkit)))
    events = SimpleNamespace(closing=Event(should_lock=True), closed=Event())
    replies = []

    def evaluate(script, callback=None):
        assert threading.current_thread() is not appkit.thread
        if callback is None:
            return BOOT
        replies.append(script)
        threading.Thread(target=callback, args=({"ok": True, "panes": 1},), daemon=True).start()

    def close():  # NSWindow.close: never consults windowShouldClose:
        appkit.close_results.append("closed")
        events.closed.set()
        appkit.queue.put(None)  # NSApp.stop_ once the last window closes

    def perform_close(_sender):  # the close button: windowShouldClose: -> closing
        assert threading.current_thread() is appkit.thread
        if events.closing.set():
            appkit.close_results.append("cancelled")
        else:
            close()

    window = SimpleNamespace(events=events, evaluate_js=Mock(side_effect=evaluate),
                             native=SimpleNamespace(performClose_=perform_close),
                             destroy=Mock(side_effect=lambda: appkit.addOperationWithBlock_(close)))
    monkeypatch.setattr(app, "_dlog", Mock())
    api = app.Api(None)
    api._close_coordinator = guard = app.WindowCloseCoordinator(api, window)
    events.closing += guard.on_closing
    events.closed += guard.cancel_preparation
    monkeypatch.setattr(app, "_gui_smoke_native_keyboard", Mock(return_value={"status": "not_assessed"}))
    thread = threading.Thread(target=app.gui_smoke, args=(api, window), daemon=True)
    thread.start()
    assert appkit.loop_ended.wait(5)
    final = dict(api._gui_smoke_report)
    assert final["ok"] is True and final["close"]["path"] == "coordinated"
    assert appkit.close_results == ["cancelled", "closed"]
    assert len(replies) == 1 and "nd2wsiPrepareForUpdate" in replies[0]
    window.destroy.assert_called_once()
    thread.join(5)
