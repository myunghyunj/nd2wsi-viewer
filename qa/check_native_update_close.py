"""Check the update-preparation close veto in one new private Agent window, without OS input.

Release note (2.5.5): on macOS, closing a window while an update is being
prepared is refused with a notice.

What runs, all inside this Agent window's own process:

1. A private synthetic slide is served and opened (never research data).
2. One real export lease of that slide is held, so the real
   ``Api.update_block_reason()`` reports an export in flight.  This keeps the
   update preparation in its export drain: it can never reach the shared
   install gate, ``stop_server_for_update`` or the install continuation.
3. Update preparation is entered on AppKit's main thread through the real
   Sparkle delegate callback ``updater_shouldPostponeRelaunchForUpdate_
   untilInvokingBlock_`` wrapping ``app.UpdateShutdownCoordinator`` (the same
   pair ``_install_app_updater`` builds).  Sparkle.framework is never loaded
   (the check fails if it is), so the delegate class is built with a stand-in
   protocol object.  With ND2WSI_QA_DELEGATE=0 ``prepare_for_update`` is called
   directly; if the delegate cannot be built without that variable, the run
   still goes through ``prepare_for_update`` for evidence but fails, and
   ``entry_point_kind`` says which entry ran.  The install block is a recorder
   that must never run.
4. A close is requested exactly as the close button / Cmd-W does:
   ``WindowCloseCoordinator.request_native_close()`` queues
   ``NSWindow.performClose:`` on the main queue, which reaches
   ``windowShouldClose:`` and pywebview's ``closing`` event.  The window must
   stay open and the veto's notice must reach the shell.
5. The update is aborted through the delegate's ``updater_didAbortWithError_``
   (Sparkle's abort callback), the lease is released, and the same close
   request is repeated as the negative control: it must run the save
   handshake and close the window.

Expect three system alert sounds: AppKit's ``performClose:`` beeps whenever
``windowShouldClose:`` refuses, which happens for both vetoed closes and once
more for the negative control (its guard refuses until saves are confirmed,
then closes the window itself).  No OS key or mouse event is ever sent.
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from pathlib import Path

os.environ["ND2WSI_DISABLE_UPDATES"] = "1"
os.environ["ND2WSI_APP_NAME"] = "nd2wsi-viewer Update Close QA"

import numpy as np  # noqa: E402
import webview  # noqa: E402
import zarr  # noqa: E402

import nd2wsi  # noqa: E402
from nd2wsi import app, updater, window_lifecycle  # noqa: E402
from nd2wsi.convert import build_group_attrs  # noqa: E402
from nd2wsi.reader import ChannelInfo, PlaneSource  # noqa: E402
from nd2wsi.translucent_smoke import bridge_result, pane_eval, wait_for  # noqa: E402
from nd2wsi.window_sessions import create_window_session  # noqa: E402

qa = Path(os.environ["ASTROMAP_QA_ROOT"]).resolve()
report_path = qa / os.environ.get("ASTROMAP_QA_REPORT", "update-close-native-check.json")
session = create_window_session("agent", qa / "sessions")
api, window = app.create_app_window(None, window_session=session)

NOTICE = window_lifecycle.WindowCloseCoordinator._UPDATE_PREPARING_NOTICE
# The text pinned by tests/test_shutdown_ownership.py at the 2.5.5 candidate.
RELEASE_NOTICE = ("An update is waiting to install, so this window can't be closed now. "
                  "The viewer will quit and reopen by itself when the update starts.")
EXPORT_WAIT = "Waiting for 1 export to finish…"
VETO_LOG = f"Window remains open: {NOTICE}"
HOLD_SECONDS = float(os.environ.get("ND2WSI_QA_HOLD_SECONDS", "3"))
T0 = time.monotonic()

report = {
    "ok": False,
    "module": nd2wsi.__file__,
    "session": session.as_dict(),
    "scope": "update-preparation-close-veto",
    "phase": "starting",
    "close_path": ("WindowCloseCoordinator.request_native_close -> main queue -> NSWindow.performClose: "
                   "-> windowShouldClose: -> pywebview events.closing -> WindowCloseCoordinator.on_closing"),
    "expected_notice": NOTICE,
    "notice_matches_release_text": NOTICE == RELEASE_NOTICE,
}
report_lock = threading.Lock()
finished = threading.Event()
cleanup_lock = threading.Lock()
# Serialises the preparation entry with cleanup: once cleanup has started, a
# main-queue entry that on_main() gave up waiting for must not start a worker.
entry_lock = threading.Lock()
ctx = {"update": None, "delegate": None, "lease": None, "aborted": False, "abandoned": False}
closing_calls: list[dict] = []
close_logs: list[dict] = []
update_logs: list[dict] = []
sparkle_logs: list[dict] = []
install_calls: list[dict] = []
direct_failures: list[dict] = []


def elapsed():
    return round(time.monotonic() - T0, 3)


def is_main():
    return threading.current_thread() is threading.main_thread()


def check(value, message):
    if not value:
        raise AssertionError(message)


def phase(name):
    report["phase"] = name
    report.setdefault("timings", {})[name] = elapsed()


def write_report():
    report["exports_report"] = str(session.exports_root / "update-close-native-report.json")
    payload = None
    for _ in range(3):
        try:
            with report_lock:
                payload = json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n"
            break
        except RuntimeError:  # a late daemon thread appended while serialising
            time.sleep(.1)
    if payload is None:
        payload = json.dumps({"ok": False, "session": session.as_dict(),
                              "error": "report could not be serialised"}) + "\n"
    (session.exports_root / "update-close-native-report.json").write_text(payload, encoding="utf-8")
    report_path.write_text(payload, encoding="utf-8")
    print(payload, flush=True)


def capture_log(coordinator, sink):
    """Record a coordinator's diagnostic lines and still send them to the app log."""
    original = coordinator._log

    def log(message):
        sink.append({"t": elapsed(), "message": str(message)})
        original(message)

    coordinator._log = log


def sparkle_log(message):
    sparkle_logs.append({"t": elapsed(), "message": str(message)})
    app._dlog(message)


def install_handler():
    # Sparkle's install block. Reaching it would mean preparation finished,
    # released the server and asked Sparkle to install: the check fails.
    install_calls.append({"t": elapsed(), "main_thread": is_main()})


def direct_failure(message):
    direct_failures.append({"t": elapsed(), "message": str(message)})


def closing_observer():
    """Registered after the guard on events.closing; reads plain state only.

    pywebview runs closing handlers in order on the main thread, so this sees
    what on_closing just decided. It returns None, which never vetoes.
    """
    close = api._close_coordinator
    closing_calls.append({
        "t": elapsed(),
        "phase": report.get("phase"),
        "main_thread": is_main(),
        "api_status": api._status,
        "close_request_running": bool(close._running),
        "close_approved": bool(close._approved),
        "update_holds_panes": close._ownership().held_by_other(close),
        "veto_log_lines": veto_log_count(),
    })


def veto_log_count():
    return sum(1 for entry in close_logs if entry["message"] == VETO_LOG)


def on_main(operation, *, timeout=10.0, what="main-thread operation"):
    """Run ``operation`` on AppKit's main queue, as the app's own dispatch does."""
    from Foundation import NSOperationQueue

    done, box = threading.Event(), {}

    def run_it():
        try:
            box["value"] = operation()
        except BaseException as exc:  # re-raised on the calling thread
            box["error"] = exc
        finally:
            done.set()

    NSOperationQueue.mainQueue().addOperationWithBlock_(run_it)
    if not done.wait(timeout):
        raise TimeoutError(f"{what} did not run on the main thread within {timeout:g} s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def native_window_state():
    from webview.platforms.cocoa import BrowserView

    def probe():
        native = window.native
        return {
            "main_thread": is_main(),
            "visible": bool(native.isVisible()) if native is not None else None,
            "browser_instance": window.uid in BrowserView.instances,
            "closed_event": window.events.closed.is_set(),
        }

    state = on_main(probe, what="native window probe")
    state["open"] = bool(state["browser_instance"] and not state["closed_event"])
    return state


NOTICE_RECORDER = """(() => {
  if (typeof window.nd2wsiUpdateNotice !== 'function') return false;
  if (!window.__qaNoticeLog) {
    const original = window.nd2wsiUpdateNotice;
    window.__qaNoticeLog = [];
    window.nd2wsiUpdateNotice = (message) => {
      window.__qaNoticeLog.push({text: String(message), at: Date.now()});
      return original(message);
    };
  }
  return true;
})()"""

SHELL_STATE = """(() => {
  const toast = document.getElementById('shell-toast');
  return {
    frozen: document.documentElement.classList.contains('preparing-update'),
    requestId: typeof quitPreparationRequestId === 'string' ? quitPreparationRequestId : null,
    readyFrames: typeof readyFrames === 'object' ? readyFrames.size : null,
    toast: toast ? toast.textContent : null,
    toastShown: !!(toast && toast.classList.contains('show')),
    notices: (window.__qaNoticeLog || []).map(entry => entry.text),
  };
})()"""

TOAST_STATE = """(() => {
  const toast = document.getElementById('shell-toast');
  return {text: toast ? toast.textContent : null, shown: !!(toast && toast.classList.contains('show'))};
})()"""


def shell_notices():
    return window.evaluate_js("(window.__qaNoticeLog || []).map(entry => entry.text)") or []


def snapshot(label):
    close = api._close_coordinator
    update = ctx["update"]
    data = {
        "t": elapsed(),
        "api_status": api._status,
        "update_block_reason": api.update_block_reason(),
        "server_alive": api._httpd is not None,
        "update_running": bool(update is not None and update._running),
        "update_wire_request_id": update._wire_request_id if update is not None else None,
        "update_teardown_started": update._teardown_started if update is not None else None,
        "update_holds_panes": close._ownership().held_by_other(close),
        "close_request_running": bool(close._running),
        "close_approved": bool(close._approved),
        "veto_log_lines": veto_log_count(),
        "install_handler_calls": len(install_calls),
        "shell": window.evaluate_js(SHELL_STATE),
        "pane_frozen": pane_eval(window, "state.quitPreparing === true"),
        "native_window": native_window_state(),
    }
    report.setdefault("snapshots", {})[label] = data
    return data


def synthetic_store():
    """Build only private QA pixels; never open/rebuild a research-data cache."""
    yy, xx = np.indices((1024, 1536))
    raw = np.stack(((xx * 3 + yy) % 4096, (yy * 5 + 700) % 4096)).astype(np.uint16)
    source = PlaneSource(data=None, dtype=raw.dtype, shape=raw.shape, rgb=False,
                         channels=[ChannelInfo("QA FITC", (0, 255, 80)), ChannelInfo("QA DAPI", (60, 100, 255))],
                         pixel_size_um=(.5, .5), source_name="update-close-QA.nd2",
                         selection={"t": 0, "p": 0, "z": 0})
    levels = [raw[:, ::factor, ::factor] for factor in (1, 2, 4, 8)]
    attrs = build_group_attrs(source, [data.shape[1:] for data in levels], 256,
                              [dict(start=0, end=4095, min=0, max=4095)] * 2)
    store = session.exports_root / "synthetic.ome.zarr"
    group = zarr.open_group(str(store), mode="w")
    for index, data in enumerate(levels):
        group.create_array(str(index), data=data, chunks=(1, 256, 256))
    group.attrs.update(attrs)
    return store


def sparkle_loaded():
    try:
        import objc

        objc.lookUpClass("SPUStandardUpdaterController")
        return True
    except Exception:
        return False


def build_entry(update):
    """The real Nd2wsiUpdaterDelegate around the coordinator, without Sparkle.framework."""
    entry = {
        "coordinator_class": f"{type(update).__module__}.{type(update).__qualname__}",
        "sparkle_framework_loaded_before": sparkle_loaded(),
        "app_updater_coordinator": repr(app._UPDATER_COORDINATOR),
        "api_updater": repr(api._updater),
        "updates_disabled_for_agent": bool(api._updates_disabled),
    }
    if os.environ.get("ND2WSI_QA_DELEGATE", "1") == "0":
        entry.update(kind="coordinator.prepare_for_update", reason="ND2WSI_QA_DELEGATE=0")
        return None, entry
    try:
        import objc

        try:
            protocol = objc.protocolNamed("SPUUpdaterDelegate")
            entry["protocol"] = "SPUUpdaterDelegate (already registered in this process)"
        except Exception:
            # Loading Sparkle.framework is deliberately avoided. The protocol
            # object only declares conformance; the delegate's methods are the
            # shipped Python implementation either way.
            protocol = objc.formal_protocol("Nd2wsiQAUpdaterDelegateStandIn", None, [])
            entry["protocol"] = "Nd2wsiQAUpdaterDelegateStandIn (stand-in; Sparkle.framework not loaded)"
        delegate_type = updater._delegate_class(protocol)
        delegate = delegate_type.alloc().initWithCoordinator_logger_(update, sparkle_log)
        entry.update(kind="Nd2wsiUpdaterDelegate.updater_shouldPostponeRelaunchForUpdate_untilInvokingBlock_",
                     delegate_class=delegate_type.__name__,
                     abort_entry="Nd2wsiUpdaterDelegate.updater_didAbortWithError_")
        return delegate, entry
    except Exception as exc:
        entry.update(kind="coordinator.prepare_for_update",
                     delegate_error=f"{type(exc).__name__}: {exc}")
        return None, entry


def abort_update(*, main_queue):
    """Sparkle's abort: the delegate cancels the coordinator. Idempotent."""
    update, delegate = ctx["update"], ctx["delegate"]
    if update is None or ctx["aborted"]:
        return None
    message = "nd2wsi QA: simulated Sparkle abort"
    if delegate is not None:
        operation = lambda: (delegate.updater_didAbortWithError_(None, message), is_main())[1]  # noqa: E731
    else:
        operation = lambda: (update.cancel_preparation(), is_main())[1]  # noqa: E731
    result = on_main(operation, what="Sparkle abort") if main_queue else operation()
    ctx["aborted"] = True
    return result


def cleanup_preparation():
    """Cancel before the lease is released, so the drain never reaches the install gate."""
    with cleanup_lock:
        # A queued entry that runs after this point refuses to prepare, so
        # ``update._running`` below cannot turn true once the lease is released.
        with entry_lock:
            ctx["abandoned"] = True
        update = ctx["update"]
        stopped = True
        if update is not None:
            try:
                abort_update(main_queue=False)
            except Exception:
                report["cleanup_abort_error"] = traceback.format_exc()
            # Idempotent: it only sets the current request's cancel event.
            update.cancel_preparation()
            deadline = time.monotonic() + 15
            while update._running and time.monotonic() < deadline:
                time.sleep(.05)
            stopped = not update._running
        lease, ctx["lease"] = ctx["lease"], None
        if lease is not None:
            if stopped:
                lease.__exit__(None, None, None)
            else:
                ctx["lease"] = lease
                report["export_lease_left_held"] = "the update worker had not finished; the lease ends with the process"


def request_close(label):
    """One close-button close through the app's own close guard."""
    close = api._close_coordinator
    before = {"closing_calls": len(closing_calls), "veto_log_lines": veto_log_count(),
              "shell_notice_count": shell_notices().count(NOTICE), "t": elapsed()}
    close.request_native_close()
    wait_for(lambda: len(closing_calls) > before["closing_calls"], timeout=10,
             description=f"{label}: the closing event (performClose: -> windowShouldClose:)")
    return before, closing_calls[before["closing_calls"]]


def run():
    try:
        phase("identity")
        context = api.window_context()
        check(context["role"] == "agent" and context["id"] == session.id, f"Wrong Agent window identity: {context}")
        wait_for(lambda: window.evaluate_js("!!window.pywebview?.api"), description="Agent bridge")
        bridged = bridge_result(window, "pywebview.api.window_context()")
        check(bridged["role"] == "agent" and bridged["id"] == session.id, f"Bridge identity mismatch: {bridged}")
        report["bridge_identity_verified"] = True

        close = getattr(api, "_close_coordinator", None)
        check(isinstance(close, window_lifecycle.WindowCloseCoordinator), "No native close guard in this window")
        handlers = list(getattr(window.events.closing, "_items", []))
        check(close.on_closing in handlers, "The close guard is not registered on pywebview's closing event")
        window.events.closing += closing_observer
        report["closing_handlers"] = [getattr(h, "__qualname__", repr(h)) for h in window.events.closing._items]
        capture_log(close, close_logs)

        phase("slide")
        store = synthetic_store()
        api._httpd, url = app.start_server(store, window_session=session)
        window.load_url(url)
        wait_for(lambda: pane_eval(window, "!!(state.info && state.annReady && state.viewer.isOpen())"),
                 timeout=60, description="private synthetic slide")
        wait_for(lambda: window.evaluate_js("typeof readyFrames === 'object' && readyFrames.size === 1"),
                 description="the shell's ready pane")
        check(window.evaluate_js(NOTICE_RECORDER), "The shell has no nd2wsiUpdateNotice")
        slides = api._httpd.registry.slides
        check(len(slides) == 1, f"Expected one private slide, found {len(slides)}")
        state = next(iter(slides.values()))
        baseline = snapshot("baseline")
        check(baseline["native_window"]["open"], f"Agent window is not open: {baseline['native_window']}")
        check(baseline["update_block_reason"] is None and not baseline["update_holds_panes"],
              f"Unexpected update state before the check: {baseline}")
        check(not baseline["shell"]["frozen"] and baseline["pane_frozen"] is False,
              f"Panes frozen before the check: {baseline}")
        check(baseline["api_status"] != NOTICE, "The veto notice is already the status before any close")

        phase("hold-export")
        # The same lease a running export holds (server.py `with st.exports`).
        state.exports.__enter__()
        ctx["lease"] = state.exports
        held_reason = api.update_block_reason()
        report["export_lease"] = {"reason_while_held": held_reason}
        check(held_reason == EXPORT_WAIT, f"Export lease did not block the update: {held_reason!r}")

        phase("prepare")
        update = app.UpdateShutdownCoordinator(api, window)
        capture_log(update, update_logs)
        delegate, entry = build_entry(update)
        ctx["update"], ctx["delegate"] = update, delegate
        report["entry_point"] = entry
        report["entry_point_kind"] = entry["kind"]

        def postpone():
            with entry_lock:
                if ctx["abandoned"]:
                    # on_main() timed out and cleanup has run (or is running):
                    # starting a worker now could reach the shared install gate.
                    report["late_preparation_entry_refused_at"] = elapsed()
                    return {"returned": False, "returned_repr": "abandoned", "main_thread": is_main()}
                if delegate is not None:
                    value = delegate.updater_shouldPostponeRelaunchForUpdate_untilInvokingBlock_(
                        None, None, install_handler)
                else:
                    value = update.prepare_for_update(install_handler, direct_failure)
                return {"returned": bool(value), "returned_repr": repr(value), "main_thread": is_main()}

        entered = on_main(postpone, what="update preparation entry")
        entry.update(entered)
        check(entered["returned"] and entered["main_thread"], f"Preparation was not accepted: {entered}")
        wait_for(lambda: update._running and close._ownership().held_by_other(close), timeout=10,
                 description="the update to own this window's panes")
        wait_for(lambda: EXPORT_WAIT in shell_notices(), timeout=45,
                 description="the update's export-drain notice in the shell")
        preparing = snapshot("preparing")
        check(preparing["shell"]["frozen"] and preparing["pane_frozen"] is True,
              f"The update did not freeze the shell and pane: {preparing}")
        check(preparing["shell"]["requestId"] and preparing["shell"]["requestId"] == preparing["update_wire_request_id"],
              f"The shell is not frozen under the update's request: {preparing}")
        check(preparing["server_alive"] and not preparing["update_teardown_started"] and not install_calls,
              f"Preparation went past the export drain: {preparing}")
        check(any("annotations saved" in line["message"] for line in update_logs),
              f"The update's save handshake did not complete: {update_logs}")

        phase("veto")
        before, call = request_close("veto")
        report["veto_close"] = {"before": before, "closing_event": call}
        check(call["main_thread"], f"closing ran off the main thread: {call}")
        check(call["api_status"] == NOTICE and not call["close_request_running"] and call["update_holds_panes"],
              f"on_closing did not refuse for the update: {call}")
        wait_for(lambda: shell_notices().count(NOTICE) == before["shell_notice_count"] + 1, timeout=10,
                 description="the veto notice in the shell")
        toast = window.evaluate_js(TOAST_STATE)
        report["veto_close"]["toast_on_arrival"] = toast
        check(toast["text"] == NOTICE and toast["shown"], f"The veto notice is not the visible toast: {toast}")
        time.sleep(1.0)
        vetoed = snapshot("vetoed")
        check(vetoed["native_window"]["open"], f"The window closed while an update was prepared: {vetoed}")
        check(vetoed["native_window"]["visible"] == baseline["native_window"]["visible"],
              f"Window visibility changed after the refused close: {vetoed['native_window']}")
        check(vetoed["api_status"] == NOTICE, f"Status is not the veto notice: {vetoed['api_status']!r}")
        check(vetoed["veto_log_lines"] == before["veto_log_lines"] + 1, f"Veto log line missing: {close_logs}")
        check(not vetoed["close_request_running"] and not vetoed["close_approved"],
              f"The refused close started or approved a save request: {vetoed}")
        check(vetoed["update_running"] and vetoed["update_holds_panes"], f"The update lost its panes: {vetoed}")
        check(vetoed["shell"]["frozen"] and vetoed["pane_frozen"] is True
              and vetoed["shell"]["requestId"] == preparing["shell"]["requestId"],
              f"The refused close unlocked or retargeted the frozen panes: {vetoed}")
        check(vetoed["server_alive"] and not install_calls, f"The refused close released the server: {vetoed}")
        report["notice"] = {"observed_toast": toast["text"], "api_status": vetoed["api_status"],
                            "expected": NOTICE, "shell_notices": vetoed["shell"]["notices"]}

        phase("repeat-veto")
        time.sleep(HOLD_SECONDS)
        before, call = request_close("repeat veto")
        report["repeat_veto_close"] = {"held_seconds": HOLD_SECONDS, "before": before, "closing_event": call}
        check(call["main_thread"] and not call["close_request_running"] and call["update_holds_panes"],
              f"The repeated close was not refused for the update: {call}")
        wait_for(lambda: shell_notices().count(NOTICE) == before["shell_notice_count"] + 1, timeout=10,
                 description="the repeated veto notice in the shell")
        repeat = snapshot("repeat_vetoed")
        check(repeat["native_window"]["open"] and repeat["veto_log_lines"] == before["veto_log_lines"] + 1,
              f"The repeated close was not vetoed: {repeat}")
        check(repeat["shell"]["frozen"] and repeat["pane_frozen"] is True and repeat["update_running"]
              and repeat["shell"]["requestId"] == preparing["shell"]["requestId"] and not repeat["close_request_running"],
              f"The update's frozen state changed after the hold: {repeat}")

        phase("cancel")
        report["abort_on_main_thread"] = abort_update(main_queue=True)
        wait_for(lambda: not update._running and not close._ownership().held_by_other(close), timeout=20,
                 description="the aborted update to release this window")
        wait_for(lambda: window.evaluate_js(
            "!document.documentElement.classList.contains('preparing-update') && quitPreparationRequestId === ''"),
            timeout=10, description="the shell to unlock after the abort")
        wait_for(lambda: pane_eval(window, "state.quitPreparing === false"), timeout=10,
                 description="the pane to unlock after the abort")
        cleanup_preparation()
        cancelled = snapshot("cancelled")
        check(cancelled["update_block_reason"] is None, f"The export lease is still counted: {cancelled}")
        check(not cancelled["update_running"] and not cancelled["update_holds_panes"], f"Update still active: {cancelled}")
        check(cancelled["server_alive"] and cancelled["update_teardown_started"] is False and not install_calls,
              f"The aborted update tore the window down or resumed installation: {cancelled}")
        check(cancelled["native_window"]["open"], f"The window closed during the abort: {cancelled}")
        if delegate is not None:
            check(any(line["message"].startswith("Sparkle update aborted") for line in sparkle_logs),
                  f"The delegate's abort callback did not run: {sparkle_logs}")
        report["export_lease"]["reason_after_release"] = cancelled["update_block_reason"]

        phase("negative-control-close")
        listener_calls = []
        closed_at = []
        close.add_close_listener(lambda: listener_calls.append(
            {"t": elapsed(), "main_thread": is_main(), "approved": bool(close._approved)}))
        window.events.closed += lambda: closed_at.append(elapsed())
        before, call = request_close("negative control")
        control = report["negative_control"] = {"before": before, "closing_event": call,
                                                "listener_calls": listener_calls}
        check(call["main_thread"] and call["close_request_running"] and not call["update_holds_panes"],
              f"Without an update the close guard did not start its save handshake: {call}")
        wait_for(lambda: listener_calls, timeout=60, description="the coordinated close to be approved")
        control["closed_event"] = window.events.closed.wait(20)
        check(control["closed_event"], "The approved close did not close the window")
        from webview.platforms.cocoa import BrowserView

        wait_for(lambda: window.uid not in BrowserView.instances, timeout=10,
                 description="pywebview to drop the closed window")
        control.update(
            seconds=round(time.monotonic() - T0 - before["t"], 2),
            closed_at=closed_at,
            close_approved=bool(close._approved),
            veto_log_lines_after=veto_log_count(),
            closing_event_calls_after=len(closing_calls) - before["closing_calls"],
            install_handler_calls=len(install_calls),
            api_status_after=api._status,
        )
        check(listener_calls[0]["main_thread"] and listener_calls[0]["approved"] and control["close_approved"],
              f"The close was not approved by the guard on the main thread: {control}")
        check(control["veto_log_lines_after"] == before["veto_log_lines"],
              f"The negative-control close was vetoed: {close_logs}")
        check(not install_calls, "Sparkle's install block ran")
        report["sparkle_framework_loaded_after"] = sparkle_loaded()
        check(not entry.get("sparkle_framework_loaded_before") and not report["sparkle_framework_loaded_after"],
              f"Sparkle.framework was loaded in the QA process (before={entry.get('sparkle_framework_loaded_before')}, "
              f"after={report['sparkle_framework_loaded_after']})")
        check(report["notice_matches_release_text"],
              f"The veto notice differs from the 2.5.5 release text: {NOTICE!r}")
        check(delegate is not None or os.environ.get("ND2WSI_QA_DELEGATE") == "0",
              f"Sparkle delegate entry unavailable (rerun with ND2WSI_QA_DELEGATE=0 to accept "
              f"the direct coordinator entry): {entry}")
        report["ok"] = True
    except Exception:
        report["error"] = traceback.format_exc()
    finally:
        try:
            cleanup_preparation()
        except Exception:
            report["cleanup_error"] = traceback.format_exc()
        try:
            if not window.events.closed.is_set():
                # A failed check must never leave the Agent window held by the veto.
                reason = f"update-close check failed in phase {report.get('phase')}"
                report["forced_close"] = reason
                report["ok"] = False
                try:
                    # Provisional: if the window cannot be closed, the GUI loop never
                    # returns and the final write below never happens.
                    write_report()
                except Exception:
                    report["provisional_report_error"] = traceback.format_exc()
                coordinator = getattr(api, "_close_coordinator", None)
                if coordinator is not None:
                    coordinator.approve_close(reason)
                window.destroy()
        except Exception:
            report["forced_close_error"] = traceback.format_exc()
            try:
                # pywebview's own Cocoa close (NSWindow.close) without destroy()'s
                # 20 s wait for the shown event; it skips windowShouldClose:.
                from webview.platforms.cocoa import destroy_window

                destroy_window(window.uid)
                report["forced_close_fallback"] = "webview.platforms.cocoa.destroy_window"
            except Exception:
                report["forced_close_fallback_error"] = traceback.format_exc()
        finally:
            finished.set()


gui_returned = False
try:
    webview.start(run, private_mode=True)
    gui_returned = True
except Exception:
    report["error"] = (report.get("error") or "") + traceback.format_exc()
finally:
    try:
        if not finished.wait(20):
            report.setdefault("error", f"The GUI loop ended before the check finished (phase {report.get('phase')})")
            report["ok"] = False
            try:
                cleanup_preparation()
            except Exception:
                report["cleanup_error"] = traceback.format_exc()
        report["gui_loop_returned"] = gui_returned
        report["ok"] = bool(report.get("ok")) and gui_returned and finished.is_set()
        report["evidence"] = {
            "closing_event_calls": list(closing_calls),
            "close_guard_log": list(close_logs),
            "update_coordinator_log": list(update_logs),
            "sparkle_delegate_log": list(sparkle_logs),
            "direct_failures": list(direct_failures),
            "install_handler_calls": list(install_calls),
        }
        write_report()
    finally:
        if api._httpd is not None:
            api.stop_server_for_update()
        session.mark_closed()
