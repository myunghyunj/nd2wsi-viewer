"""Finder event registration without a native window or slide I/O."""

import json
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nd2wsi import app


@pytest.fixture
def cocoa(monkeypatch):
    allocations, assignments = [], []

    class AppDelegate:
        @classmethod
        def alloc(cls):
            delegate = cls()
            allocations.append(delegate)
            return delegate

        def init(self):
            return self

    native_app = SimpleNamespace(current=None, on_bind=None, replyToOpenOrPrint_=Mock())

    def bind(delegate):
        native_app.current = delegate
        assignments.append(delegate)
        if native_app.on_bind:
            native_app.on_bind(delegate)

    native_app.delegate = lambda: native_app.current
    native_app.setDelegate_ = bind
    browser = SimpleNamespace(AppDelegate=AppDelegate, app=native_app, _shared_app_delegate=None)

    def add_methods(cls, methods):
        for method in methods:
            setattr(cls, method.__name__, method)

    monkeypatch.setitem(sys.modules, "objc", SimpleNamespace(
        classAddMethods=add_methods, selector=lambda method, **_: method,
    ))
    monkeypatch.setitem(sys.modules, "webview.platforms.cocoa", SimpleNamespace(BrowserView=browser))
    dispatch = Mock()
    monkeypatch.setattr(app, "_dispatch_open", dispatch)
    monkeypatch.setattr(app, "_dlog", Mock())
    return SimpleNamespace(browser=browser, native=native_app, allocations=allocations,
                           assignments=assignments, dispatch=dispatch)


def test_finder_batch_is_accepted_as_soon_as_delegate_binds_before_any_window(cocoa):
    paths = ["/isolated/1-2.nd2", "/isolated/한글.SVS", "/isolated/cache.nd2svs", "/isolated/whole slide.SDPC", "/isolated/notes.txt"]
    cocoa.native.on_bind = lambda delegate: delegate.application_openFiles_(cocoa.native, paths)

    app._install_open_files_handler()

    delegate = cocoa.browser._shared_app_delegate
    assert delegate is not None
    assert cocoa.native.delegate() is delegate
    assert cocoa.allocations == cocoa.assignments == [delegate]
    cocoa.dispatch.assert_called_once_with(paths[:4])
    cocoa.native.replyToOpenOrPrint_.assert_called_once_with(0)


def test_reuses_pywebview_shared_delegate_without_allocating_a_replacement(cocoa):
    shared = cocoa.browser.AppDelegate()
    cocoa.browser._shared_app_delegate = shared

    app._install_open_files_handler()
    app._install_open_files_handler()

    assert cocoa.browser._shared_app_delegate is cocoa.native.delegate() is shared
    assert cocoa.allocations == []
    assert cocoa.assignments == [shared]


def test_repeated_install_keeps_delegate_identity(cocoa):
    app._install_open_files_handler()
    shared = cocoa.browser._shared_app_delegate

    app._install_open_files_handler()

    assert cocoa.browser._shared_app_delegate is cocoa.native.delegate() is shared
    assert cocoa.allocations == cocoa.assignments == [shared]


@pytest.mark.parametrize("installed_first", [False, True])
def test_does_not_steal_an_existing_application_delegate(cocoa, installed_first):
    if installed_first:
        app._install_open_files_handler()
    shared = cocoa.browser._shared_app_delegate
    previous_assignments = list(cocoa.assignments)
    other_delegate = object()
    cocoa.native.current = other_delegate

    app._install_open_files_handler()

    assert cocoa.native.delegate() is other_delegate
    assert cocoa.browser._shared_app_delegate is shared
    assert cocoa.assignments == previous_assignments


@pytest.mark.parametrize("path", ["/isolated/1-2.ND2", "/isolated/whole slide.SDPC"])
def test_single_file_handler_accepts_slides_and_rejects_other_types(cocoa, path):
    app._install_open_files_handler()
    delegate = cocoa.native.delegate()

    assert delegate.application_openFile_(cocoa.native, path) is True
    assert delegate.application_openFile_(cocoa.native, "/isolated/notes.txt") is False

    cocoa.dispatch.assert_called_once_with([path])
    cocoa.native.replyToOpenOrPrint_.assert_not_called()


class FakePage:
    """A window whose page answers the flusher like pywebview does.

    ``probe`` and ``deliver`` decide each answer; either may block, which is
    how a hung page or a page bridge that is still starting looks to the
    flusher. Every call records the thread it ran on.
    """

    def __init__(self, probe=None, deliver=None):
        self.probe = probe or (lambda: "function")
        self.deliver = deliver or (lambda batch: None)
        self.probes = 0
        self.batches = []
        self.threads = []
        self.lock = threading.Lock()

    def evaluate_js(self, script):
        with self.lock:
            self.threads.append(threading.current_thread())
        if script == "typeof window.__pydrop_multi":
            with self.lock:
                self.probes += 1
            return self.probe()
        prefix = "window.__pydrop_multi("
        assert script.startswith(prefix) and script.endswith(")")
        batch = json.loads(script[len(prefix):-1])
        with self.lock:
            self.batches.append(batch)
        return self.deliver(batch)


def wait_until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out waiting"
        time.sleep(0.005)


@pytest.fixture
def finder(monkeypatch):
    """Fresh open queue state and a fake ``webview`` module with no window."""
    gates = []  # events that blocking fakes wait on; all are set at teardown
    fake_webview = SimpleNamespace(windows=[])
    monkeypatch.setitem(sys.modules, "webview", fake_webview)
    monkeypatch.setattr(app, "_PENDING_OPENS", [])
    monkeypatch.setattr(app, "_OPENS_LOCK", threading.Lock())
    monkeypatch.setattr(app, "_FLUSHER_ALIVE", False)
    monkeypatch.setattr(app, "_LAUNCH_PATH", None)
    monkeypatch.setattr(app, "_LAUNCH_AT", 0.0)
    monkeypatch.setattr(app, "_OPEN_READY_TIMEOUT", 5.0)
    monkeypatch.setattr(app, "_OPEN_CALL_TIMEOUT", 5.0)
    monkeypatch.setattr(app, "_dlog", Mock())

    def gate():
        event = threading.Event()
        gates.append(event)
        return event

    def idle():
        return not app._FLUSHER_ALIVE

    state = SimpleNamespace(webview=fake_webview, gate=gate, idle=idle)
    yield state
    for event in gates:
        event.set()
    wait_until(idle)


def delivered(page):
    return [path for batch in page.batches for path in batch]


def test_open_queued_while_a_batch_is_delivered_is_delivered(finder):
    page = FakePage()

    def deliver(batch):
        if batch == ["/isolated/a.nd2"]:
            # Finder hands over another file while the first batch is in the page.
            app._dispatch_open(["/isolated/b.nd2"])

    page.deliver = deliver
    finder.webview.windows.append(page)

    app._dispatch_open(["/isolated/a.nd2"])
    wait_until(lambda: finder.idle() and len(page.batches) == 2)

    assert page.batches == [["/isolated/a.nd2"], ["/isolated/b.nd2"]]
    assert app._PENDING_OPENS == []


def test_concurrent_opens_are_each_delivered_exactly_once(finder):
    page = FakePage(deliver=lambda batch: time.sleep(0.001))
    finder.webview.windows.append(page)
    paths = [f"/isolated/{worker}-{index}.nd2" for worker in range(6) for index in range(20)]

    def open_some(worker):
        for path in paths[worker * 20:(worker + 1) * 20]:
            app._dispatch_open([path])
            time.sleep(0.0005)

    workers = [threading.Thread(target=open_some, args=(worker,)) for worker in range(6)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    wait_until(lambda: finder.idle() and len(delivered(page)) >= len(paths))

    assert sorted(delivered(page)) == sorted(paths)
    assert app._PENDING_OPENS == []


def test_dispatch_never_calls_the_page_on_the_calling_thread(finder):
    answer = finder.gate()
    page = FakePage(probe=lambda: answer.wait() and "function")
    finder.webview.windows.append(page)

    started = time.monotonic()
    app._dispatch_open(["/isolated/a.nd2"])  # as AppKit's main thread would
    assert time.monotonic() - started < 1.0
    wait_until(lambda: page.probes == 1)
    answer.set()
    wait_until(lambda: finder.idle() and page.batches)

    assert page.batches == [["/isolated/a.nd2"]]
    assert threading.current_thread() not in page.threads


def test_hung_delivery_does_not_block_later_opens(finder, monkeypatch):
    monkeypatch.setattr(app, "_OPEN_CALL_TIMEOUT", 0.05)
    hung = finder.gate()
    page = FakePage(deliver=lambda batch: hung.wait() if batch == ["/isolated/a.nd2"] else None)
    finder.webview.windows.append(page)

    app._dispatch_open(["/isolated/a.nd2"])
    wait_until(finder.idle)  # one bounded wait, then the flusher lets go
    assert not hung.is_set()

    app._dispatch_open(["/isolated/b.nd2"])
    wait_until(lambda: finder.idle() and len(page.batches) == 2)

    # The unanswered batch may still open when the page recovers, so it is not sent twice.
    assert page.batches == [["/isolated/a.nd2"], ["/isolated/b.nd2"]]
    assert app._PENDING_OPENS == []
    app._dlog.assert_any_call("the viewer did not answer 1 open(s) within 0.05 seconds")


def test_page_that_never_answers_leaves_opens_queued_for_the_next_open(finder, monkeypatch):
    monkeypatch.setattr(app, "_OPEN_READY_TIMEOUT", 0.2)
    recovered = finder.gate()
    page = FakePage(probe=lambda: recovered.wait() and "function")
    finder.webview.windows.append(page)

    app._dispatch_open(["/isolated/a.nd2"])
    wait_until(finder.idle)

    assert page.probes == 1  # the hung probe stranded one helper thread, not one per retry
    assert page.batches == []
    assert app._PENDING_OPENS == ["/isolated/a.nd2"]
    app._dlog.assert_any_call("flush_opens timed out; 1 open(s) stay queued for the next open")

    monkeypatch.setattr(app, "_OPEN_READY_TIMEOUT", 5.0)
    recovered.set()
    app._dispatch_open(["/isolated/b.nd2"])
    wait_until(lambda: finder.idle() and page.batches)

    assert page.batches == [["/isolated/a.nd2", "/isolated/b.nd2"]]
    assert app._PENDING_OPENS == []


def test_probe_may_outlast_one_call_budget_while_the_page_bridge_starts(finder, monkeypatch):
    # pywebview holds evaluate_js for up to 20 s until the page bridge is ready.
    monkeypatch.setattr(app, "_OPEN_CALL_TIMEOUT", 0.02)
    page = FakePage(probe=lambda: time.sleep(0.2) or "function")
    finder.webview.windows.append(page)

    app._dispatch_open(["/isolated/a.nd2"])
    wait_until(lambda: finder.idle() and page.batches)

    assert page.batches == [["/isolated/a.nd2"]]
    assert page.probes == 1


def test_waits_for_the_window_and_its_drop_function(finder):
    answers = iter(["undefined", None])
    page = FakePage(probe=lambda: next(answers, "function"))

    app._dispatch_open(["/isolated/a.nd2"])
    time.sleep(0.05)
    assert page.probes == 0 and not finder.idle()  # no window yet
    finder.webview.windows.append(page)
    wait_until(lambda: finder.idle() and page.batches)

    assert page.probes == 3
    assert page.batches == [["/isolated/a.nd2"]]


def test_probe_errors_are_retried(finder):
    failures = iter([RuntimeError("bridge not ready")])

    def probe():
        error = next(failures, None)
        if error:
            raise error
        return "function"

    page = FakePage(probe=probe)
    finder.webview.windows.append(page)

    app._dispatch_open(["/isolated/a.nd2"])
    wait_until(lambda: finder.idle() and page.batches)

    assert page.batches == [["/isolated/a.nd2"]]


def test_launch_file_is_not_opened_twice_while_the_app_starts(finder, tmp_path, monkeypatch):
    launch, other = tmp_path / "launch.nd2", tmp_path / "other.nd2"
    launch.touch()
    other.touch()
    page = FakePage()
    finder.webview.windows.append(page)
    monkeypatch.setattr(app, "_LAUNCH_PATH", str(launch.resolve()))
    monkeypatch.setattr(app, "_LAUNCH_AT", time.time())

    app._dispatch_open([str(launch)])
    assert app._PENDING_OPENS == [] and finder.idle()  # nothing queued, no flusher

    app._dispatch_open([str(launch), str(other)])
    wait_until(lambda: finder.idle() and page.batches)
    assert page.batches == [[str(other)]]

    monkeypatch.setattr(app, "_LAUNCH_AT", time.time() - 16)
    app._dispatch_open([str(launch)])
    wait_until(lambda: finder.idle() and len(page.batches) == 2)
    assert page.batches[1] == [str(launch)]
