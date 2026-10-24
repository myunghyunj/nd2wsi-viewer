"""A headless WinForms-like pywebview window for native close tests.

It models only the pywebview 6 WinForms behaviour the close guard relies on:

- the Form owns one UI thread; ``InvokeRequired`` is true on every other one;
- ``BeginInvoke`` queues a delegate on that thread, ``Invoke`` runs it there
  and waits (inline when already on it);
- ``destroy()`` is ``destroy_window``: ``Invoke(Form.Close)``, which raises
  FormClosing, i.e. ``events.closing.set()``, where any handler returning
  False cancels the close;
- an uncancelled close fires ``events.closed`` on another thread and ends the
  message loop once the current UI work item returns (``Application.Exit``);
- ``events._pywebviewready`` is set once the page bridge is injected, and stays
  clear when WebView2 fails to initialise;
- ``create_confirmation_dialog`` is pywebview's MessageBox: called from a
  worker it blocks that worker until answered (True for OK).
"""

from __future__ import annotations

import inspect
import queue
import threading
from types import SimpleNamespace
from unittest.mock import DEFAULT, Mock

EVENT_NAMES = ("before_load", "closed", "closing", "loaded", "shown", "initialized")


class Event:
    """pywebview's Event: handlers run inline for locking events, else on a thread."""

    def __init__(self, *, should_lock=False):
        self.handlers = []
        self.should_lock = should_lock

    def __iadd__(self, callback):
        self.handlers.append(callback)
        return self

    def set(self, *args):
        def call(handler):
            parameters = inspect.signature(handler).parameters
            return handler() if not parameters else handler(*args)

        handlers = list(self.handlers)
        if not self.should_lock:
            threading.Thread(target=lambda: [call(handler) for handler in handlers], daemon=True).start()
            return False
        return any([call(handler) is False for handler in handlers])


class Action:
    """pythonnet's System.Action delegate wrapper."""

    def __init__(self, operation):
        self.operation = operation

    def __call__(self):
        self.operation()


FAKE_SYSTEM = SimpleNamespace(Action=Action)


class FakeForm:
    """The WinForms Form exposed as ``window.native``."""

    def __init__(self):
        self._queue = queue.Queue()
        self.errors = []
        self.begin_invokes = []
        self.loop_ended = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="fake-winforms-ui", daemon=True)
        self.thread.start()

    @property
    def InvokeRequired(self):  # noqa: N802 - .NET property name
        return threading.current_thread() is not self.thread

    def BeginInvoke(self, delegate):  # noqa: N802 - .NET method name
        if not isinstance(delegate, Action):
            raise TypeError("BeginInvoke needs a System.Action delegate")
        self.begin_invokes.append(delegate)
        self._queue.put(delegate)

    def Invoke(self, operation):  # noqa: N802 - .NET method name
        if not self.InvokeRequired:
            return operation()
        done, box = threading.Event(), {}

        def run():
            try:
                box["value"] = operation()
            except BaseException as exc:  # .NET Invoke rethrows on the caller
                box["error"] = exc
            finally:
                done.set()

        self._queue.put(run)
        assert done.wait(5), "the fake UI thread did not run an Invoke"
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def exit_loop(self):
        self._queue.put(None)

    def _loop(self):
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    return
                try:
                    item()
                except BaseException as exc:  # an unhandled UI-thread exception
                    self.errors.append(exc)
        finally:
            self.loop_ended.set()


class FakeWinFormsWindow:
    """A pywebview Window on the WinForms backend, without a renderer."""

    def __init__(self):
        self.native = FakeForm()
        self.events = SimpleNamespace(**{
            name: Event(should_lock=name in ("closing", "before_load", "initialized"))
            for name in EVENT_NAMES
        })
        self.events._pywebviewready = threading.Event()
        self.events._pywebviewready.set()
        self.is_closed = False
        self.close_attempts = 0
        self.close_results = []  # "cancelled" or "closed" per FormClosing
        self.closed_count = 0
        self.destroy_threads = []
        self.flush_reply = {"ok": True, "panes": 1}
        self.auto_reply = True
        self.pending_callbacks = []
        self.scripts = []
        self.evaluate_js = Mock(side_effect=self._evaluate)
        self.destroy = Mock(side_effect=self._destroy)
        self.dialog_answer = False
        self.dialog_threads = []
        self.create_confirmation_dialog = Mock(side_effect=self._confirm)

    # Browser -----------------------------------------------------------------
    def _evaluate(self, script, callback=None):
        # WinForms evaluate_js Invokes the UI thread and waits for a
        # continuation scheduled on it: calling it there deadlocks.
        assert self.native.InvokeRequired, "evaluate_js on the WinForms UI thread deadlocks"
        self.scripts.append(script)
        if callback is None:
            return DEFAULT
        if self.auto_reply:
            self.reply(callback, self.flush_reply)
        else:
            self.pending_callbacks.append(callback)
        return None

    @staticmethod
    def reply(callback, result):
        # Promise results arrive through the JS bridge on another thread.
        threading.Thread(target=callback, args=(result,), daemon=True).start()

    def _confirm(self, title, message):
        self.dialog_threads.append(threading.current_thread())
        return self.dialog_answer

    # Native close ------------------------------------------------------------
    def _destroy(self):
        self.destroy_threads.append(threading.current_thread())
        if self.is_closed:  # destroy_window finds no BrowserView instance
            return
        self.native.Invoke(self._form_close)

    def _form_close(self):
        """Form.Close/WM_CLOSE on the UI thread: FormClosing, then FormClosed."""
        assert not self.native.InvokeRequired
        if self.is_closed:
            return
        self.close_attempts += 1
        if self.events.closing.set():  # args.Cancel = True
            self.close_results.append("cancelled")
            return
        self.close_results.append("closed")
        self.is_closed = True
        self.closed_count += 1
        self.events.closed.set()
        self.native.exit_loop()

    def user_close(self):
        """The title-bar X, Alt+F4 or a taskbar close: WM_CLOSE on the UI thread."""
        self.native.Invoke(self._form_close)
