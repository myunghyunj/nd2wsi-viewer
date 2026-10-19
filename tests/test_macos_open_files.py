"""Finder event registration without a native window or slide I/O."""

import sys
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
