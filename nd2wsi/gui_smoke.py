from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .app import Api


def _gui_smoke_appkit_key(api: Api, window, context: dict, key: str, *, repeat: bool = False) -> dict:
    """Send one process-local key through AppKit, only to this Agent key window.

    Deliberately use NSApplication.sendEvent_ so native key equivalents and the
    responder chain participate. This is not a physical/global keyboard event.
    """
    import AppKit
    from Foundation import NSOperationQueue, NSThread

    finished, cancelled = threading.Event(), threading.Event()
    outcome = {}

    def send():
        try:
            if cancelled.is_set():
                return
            current = api.window_context()
            if (not current.get("enabled") or current.get("role") != "agent" or
                    not current.get("id") or current.get("id") != context.get("id") or
                    current.get("pid") != os.getpid() or getattr(window, "_js_api", None) is not api):
                raise RuntimeError("Native keyboard smoke requires the same bound Agent session")
            native = getattr(window, "native", None)
            application = AppKit.NSApplication.sharedApplication()
            if native is None or application.keyWindow() != native:
                raise RuntimeError("Native keyboard smoke Agent window is not the exact key window")
            number = int(native.windowNumber())
            codes = {"a": 0, "c": 8, "e": 14, "i": 34}
            modifiers = 0 if key == "e" else AppKit.NSEventModifierFlagCommand
            outcome.update(window_number=number, key=key, command=key != "e", repeat=repeat)
            for event_type in (AppKit.NSEventTypeKeyDown, AppKit.NSEventTypeKeyUp):
                if cancelled.is_set():
                    return
                if application.keyWindow() != native or getattr(window, "native", None) != native:
                    raise RuntimeError("Native keyboard smoke lost its Agent key window")
                event = AppKit.NSEvent.keyEventWithType_location_modifierFlags_timestamp_windowNumber_context_characters_charactersIgnoringModifiers_isARepeat_keyCode_(
                    event_type, (0, 0), modifiers, time.monotonic(), number, None,
                    key, key, repeat and event_type == AppKit.NSEventTypeKeyDown, codes[key])
                if event is None:
                    raise RuntimeError("AppKit did not create the smoke key event")
                application.sendEvent_(event)
        except Exception as exc:
            outcome["error"] = str(exc)
        finally:
            finished.set()

    if NSThread.isMainThread():
        send()
    else:
        NSOperationQueue.mainQueue().addOperationWithBlock_(send)
    if not finished.wait(5):
        cancelled.set()  # A late main-queue callback must never inject a key.
        raise TimeoutError("Native keyboard smoke main queue timed out")
    if outcome.get("error"):
        raise RuntimeError(outcome["error"])
    return outcome


_GUI_SMOKE_KEYBOARD_SETUP = """(() => {
  const frame = document.querySelector('#frames iframe.active');
  const view = frame && frame.contentWindow, page = frame && frame.contentDocument;
  const toolbar = document.getElementById('newtab');
  const ids = {annot:'tb-annot', channels:'tb-channels', region:'tb-region'};
  const panelNames = [...Object.keys(ids), 'info'];
  if (!page || !toolbar || Object.values(ids).some(id => !page.getElementById(id)))
    throw new Error('Native keyboard smoke controls are unavailable');
  const panels = view.eval('state.windows');
  const original = Object.fromEntries(panelNames.map(id => [id, panels[id].isHidden()]));
  const focused = document.activeElement;
  const input = document.createElement('input');
  input.id = 'nd2wsi-native-keyboard-smoke-input'; input.type = 'text';
  input.style.cssText = 'position:fixed;bottom:0;left:0;width:180px;z-index:99999';
  document.body.appendChild(input);
  const counts = Object.fromEntries(panelNames.map(id => [id, 0]));
  const listeners = Object.entries(ids).map(([id, button]) => {
    const element = page.getElementById(button), listener = () => { counts[id]++; };
    element.addEventListener('click', listener); return [element, listener];
  });
  // Info lives in the shell tools row; its pane action has no proxy button.
  const infoActions = Object.fromEntries(['open', 'close'].map(action => [action, panels.info[action]]));
  for (const [action, originalAction] of Object.entries(infoActions)) {
    panels.info[action] = function(...args) { counts.info++; return originalAction.apply(this, args); };
  }
  let events = [];
  const observe = event => { events.push({key:event.key, code:event.code, repeat:event.repeat,
    trusted:event.isTrusted, target:event.target.id || event.target.tagName}); };
  window.addEventListener('keydown', observe, true);
  window.nd2wsiNativeKeyboardSmoke = {
    prepare(editable, key) {
      events = []; window.getSelection()?.removeAllRanges(); view.getSelection()?.removeAllRanges();
      if (editable) {
        input.value = key === 'c' || key === 'e' ? '' : 'native smoke';
        input.focus(); input.setSelectionRange(0, 0);
      } else toolbar.focus();
      if (document.activeElement !== (editable ? input : toolbar))
        throw new Error('Native keyboard smoke could not focus its target');
      return this.snapshot();
    },
    snapshot() { return {counts:{...counts},
      hidden:Object.fromEntries(panelNames.map(id => [id, panels[id].isHidden()])),
      events:[...events], focused:document.activeElement.id, value:input.value,
      selection:[input.selectionStart, input.selectionEnd]}; },
    cleanup() {
      window.removeEventListener('keydown', observe, true);
      for (const [element, listener] of listeners) element.removeEventListener('click', listener);
      Object.assign(panels.info, infoActions);
      for (const [id, hidden] of Object.entries(original)) {
        if (panels[id].isHidden() !== hidden) panels[id][hidden ? 'close' : 'open']();
      }
      input.remove(); if (focused?.isConnected) focused.focus();
      delete window.nd2wsiNativeKeyboardSmoke;
      delete window.__nd2wsiNativeKeyboardSmoke;
      return true;
    },
  };
  window.__nd2wsiNativeKeyboardSmoke = window.nd2wsiNativeKeyboardSmoke;
  return {ready:true};
})()"""


def _gui_smoke_native_keyboard(api: Api, window, *, slide: bool, appkit_key=None) -> dict:
    """Bounded native menu/renderer check in the disposable macOS Agent only."""
    send_key = _gui_smoke_appkit_key if appkit_key is None else appkit_key
    report = {"status": "not_assessed", "scope": "process-local AppKit synthetic Latin keys; not physical input"}
    if sys.platform != "darwin":
        return {**report, "reason": "Native keyboard smoke is implemented for macOS only"}
    context = api.window_context()
    if not slide or not context.get("enabled") or context.get("role") != "agent":
        return {**report, "reason": "Requires a loaded slide in a fresh Agent smoke window"}
    report.update(status="failed", session_id=context.get("id"), steps=[])
    setup = False
    try:
        if not context.get("id") or context.get("pid") != os.getpid() or getattr(window, "_js_api", None) is not api:
            raise RuntimeError("Native keyboard smoke Agent identity is not bound to this window")
        window.evaluate_js(_GUI_SMOKE_KEYBOARD_SETUP)
        setup = True
        for editable in (False, True):
            for key, panel in (("a", "annot"), ("c", "channels"), ("e", "region"), ("i", "info")):
                for repeat in ((False,) if editable else (False, True)):
                    before = window.evaluate_js(
                        f"window.__nd2wsiNativeKeyboardSmoke.prepare({str(editable).lower()}, {json.dumps(key)})")
                    native = send_key(api, window, context, key, repeat=repeat)
                    # WKWebView handles native input asynchronously. Give ignored
                    # repeats/text shortcuts the same bounded observation window.
                    deadline = time.monotonic() + 0.5
                    while True:
                        after = window.evaluate_js("window.__nd2wsiNativeKeyboardSmoke.snapshot()")
                        if time.monotonic() >= deadline:
                            break
                        time.sleep(0.025)
                    toggled = not editable and not repeat
                    expected_counts = {name: count + int(toggled and name == panel)
                                       for name, count in before["counts"].items()}
                    expected_hidden = {name: (not hidden if toggled and name == panel else hidden)
                                       for name, hidden in before["hidden"].items()}
                    passed = after["counts"] == expected_counts and after["hidden"] == expected_hidden
                    if editable:
                        if key == "e":
                            passed = passed and after["value"] == "e"
                        elif key == "a":
                            passed = passed and after["selection"] == [0, len(before["value"])]
                        else:
                            passed = passed and after["value"] == before["value"]
                    report["steps"].append({**native, "focus": "editable" if editable else "shell-toolbar",
                                            "panel": panel, "passed": passed, "before": before, "after": after})
        report["status"] = "passed" if all(step["passed"] for step in report["steps"]) else "failed"
        if report["status"] == "failed":
            report["error"] = "Native keyboard routing, repeat suppression or editable protection failed"
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        if setup:
            try:
                window.evaluate_js("window.__nd2wsiNativeKeyboardSmoke.cleanup()")
            except Exception as exc:
                report.update(status="failed", cleanup_error=str(exc))
    return report


def _gui_smoke_close(api: Api, window, report: dict, *, timeout: float) -> None:
    """Close the smoke window and settle ``report`` before the GUI loop returns.

    main() writes ``api._gui_smoke_report`` as soon as ``webview.start``
    returns, which can be the moment the window closes. So every outcome is
    recorded before the close that ends the loop: by this thread before a
    direct or forced destroy, or by the coordinator's UI-thread listener after
    its approved destroy was requested and before the loop can return.
    """
    api._gui_smoke_report = report
    coordinator = getattr(api, "_close_coordinator", None)
    if coordinator is None:
        report["close"] = {"path": "direct"}
        window.destroy()
        return
    if not report.get("ok"):
        # A failed or unready viewer cannot answer the save protocol; never
        # let the veto turn a failure report into a CI timeout.
        reason = f"GUI smoke failed in phase {report.get('phase')}"
        report["close"] = {"path": "forced", "reason": reason}
        coordinator.approve_close(reason)
        window.destroy()
        return

    started = time.monotonic()
    phase = report.get("phase")
    lock = threading.Lock()
    outcome = []
    confirmed = threading.Event()

    def saved_close():
        with lock:
            if outcome:
                return
            outcome.append("coordinated")
            report.pop("error", None)
            report.update(ok=True, phase=phase, close={
                "path": "coordinated", "seconds": round(time.monotonic() - started, 2)})
        confirmed.set()

    # Exercise the real veto -> acknowledged flush -> UI-thread approval ->
    # destroy path once. Until it confirms, a closed window is a failure.
    report.update(ok=False, phase="close", error="The window closed without the save handshake",
                  close={"path": "coordinated", "confirmed": False})
    coordinator.add_close_listener(saved_close)
    try:
        coordinator.request_native_close()
    except Exception as exc:
        failure = f"Could not start the native close: {exc}"
    else:
        if confirmed.wait(timeout):
            return
        failure = f"Native close did not complete the save handshake within {timeout:g} seconds"
    with lock:
        if outcome:
            return
        outcome.append("forced")
        if api._status:
            failure += f" (last status: {api._status})"
        report.update(ok=False, phase="close", error=failure, close={
            "path": "forced", "reason": failure, "seconds": round(time.monotonic() - started, 2)})
    coordinator.approve_close(failure)
    window.destroy()


def gui_smoke(api: Api, window, *, expect_slide: bool = False, timeout: float = 90,
              close_timeout: float = 30, native_keyboard=None) -> dict:
    """Exercise the real renderer, bridge and slide DOM, then close the app."""
    run_keyboard = _gui_smoke_native_keyboard if native_keyboard is None else native_keyboard
    started = time.monotonic()
    report = {"ok": False, "platform": sys.platform, "renderer": None, "phase": "app-loading"}
    # The opt-in round trip replaces clipboard text. Enable only on disposable
    # Windows CI desktops, never during local macOS packaged verification.
    clipboard_check = sys.platform == "win32" and os.environ.get("ND2WSI_SMOKE_LUT_CLIPBOARD") == "1"
    expected = getattr(api, "_requested_handoff", None)
    try:
        import webview

        report["window_context"] = api.window_context()
        if expected is not None:
            report["handoff_requested"] = expected
            source = Path(api._launch_source).resolve()
            info = source.stat()
            report["handoff_source"] = {"path": str(source), "size": info.st_size, "mtime_ns": info.st_mtime_ns}
        report["renderer"] = webview.renderer
        if sys.platform == "win32" and webview.renderer != "edgechromium":
            raise RuntimeError("GUI smoke requires the Edge WebView2 renderer")
        while time.monotonic() - started < timeout:
            if api._status.startswith("could not open"):
                raise RuntimeError(api._status)
            result = None
            try:
                result = window.evaluate_js("""(() => {
                  const api = window.pywebview && window.pywebview.api;
                  if (!api || !api.update_status || document.readyState !== 'complete')
                    return {phase: 'app-bridge', document_ready: document.readyState};
                  if (!window.__nd2wsiSmokeBridge) {
                    window.__nd2wsiSmokeBridge = 'pending';
                    api.update_status().then(status => { window.__nd2wsiSmokeBridge = status; })
                      .catch(() => { window.__nd2wsiSmokeBridge = 'failed'; });
                  }
                  const bridge = window.__nd2wsiSmokeBridge;
                  if (!bridge || typeof bridge !== 'object' || !bridge.version)
                    return {phase: 'app-bridge', bridge_status: bridge || 'unavailable'};
                  const frame = document.querySelector('#frames iframe.active');
                  const page = frame && frame.contentDocument;
                  const canvas = page && page.querySelector('#stage canvas');
                  const plate = page && page.querySelector('#plate img');
                  const slide = !!(frame && typeof readyFrames !== 'undefined' &&
                    readyFrames.has(frame.dataset.sid) &&
                    ((canvas && canvas.width > 0 && canvas.height > 0) ||
                    (plate && plate.complete && plate.naturalWidth > 0)));
                  if (slide && !frame.__nd2wsiSmokeLut) {
                    const view = frame.contentWindow;
                    const progress = frame.__nd2wsiSmokeLut = {pending: true, phase: 'lut-toolbar'};
                    const step = async (phase, run, timeoutMs = 10000) => {
                      progress.phase = phase;
                      progress.step_timeout_ms = timeoutMs;
                      let timer;
                      try {
                        return await Promise.race([Promise.resolve().then(run), new Promise((_, reject) => {
                          timer = setTimeout(() => reject(new Error('LUT smoke ' + phase + ' timed out after ' + timeoutMs + ' ms')), timeoutMs);
                        })]);
                      } finally { clearTimeout(timer); }
                    };
                    frame.__nd2wsiSmokeLutTask = (async () => {
                      const transfer = view.Nd2LutTransfer;
                      if (!transfer || ['encode', 'planPaste', 'fitsChannel'].some(name => typeof transfer[name] !== 'function'))
                        throw new Error('Packaged LUT transfer module is unavailable');
                      const buttons = ['lut-type', 'lut-copy', 'lut-paste'].map(id => page.getElementById(id));
                      if (buttons.some(button => !button || typeof button.onclick !== 'function') ||
                          !page.getElementById('lut-dialog') || !page.getElementById('lut-copy-tooltip'))
                        throw new Error('Packaged LUT toolbar is not wired');
                      const shadingButton = document.getElementById('shading-correction');
                      if (!shadingButton || document.getElementById('update-check') ||
                          !document.getElementById('shading-progress-bar') ||
                          !document.getElementById('shading-progress-title') ||
                          !document.getElementById('shading-cancel') ||
                          !['shading-confirm-title', 'shading-confirm-persistence', 'shading-confirm-estimate',
                            'shading-confirm-continue', 'shading-confirm-cancel'].every(id => document.getElementById(id)) ||
                          typeof document.getElementById('shading-confirm')?.showModal !== 'function' ||
                          !window.Nd2ShadingCorrection || !view.Nd2ShadingCorrection ||
                          typeof window.Nd2ShadingCorrection.nextAction !== 'function' ||
                          typeof view.Nd2ShadingCorrection.nextAction !== 'function' ||
                          typeof window.Nd2ShadingCorrection.methodSupport !== 'function')
                        throw new Error('Packaged Shading Correction controls are unavailable');
                      if (document.getElementById('shading-methods') || document.getElementById('shading-confirm-message'))
                        throw new Error('Packaged Shading Correction contains obsolete method controls');
                      await step('shading-status', async () => {
                        const deadline = Date.now() + 10000;
                        while (!view.eval('state.shading && !state.shading.snapshot().refreshing && !state.shading.snapshot().view_stale && state.shading.snapshot().state !== "checking"') ||
                            !shadingStates.has(frame.dataset.sid)) {
                          if (Date.now() >= deadline) throw new Error('Shading Correction status did not arrive');
                          await new Promise(resolve => setTimeout(resolve, 50));
                        }
                      });
                      const shadingStatus = view.eval('state.shading.snapshot()');
                      if (typeof shadingStatus.applied !== 'boolean' || typeof shadingStatus.can_cancel !== 'boolean' ||
                          !(shadingStatus.operation === null || ['apply', 'revoke'].includes(shadingStatus.operation)) ||
                          shadingStatus.generation !== view.eval('state.info.generation') || shadingStatus.state === 'failed' ||
                          !shadingButton.title.includes('Shading Correction'))
                        throw new Error('Shading Correction did not initialize its committed pyramid state');
                      const shading = window.Nd2ShadingCorrection;
                      const persistentActions = {
                        apply: shading.nextAction({state: 'idle', supported: true, applied: false, methods: {bffree: {supported: true}}}),
                        revoke: shading.nextAction({state: 'ready', supported: true, applied: true, method: 'h'}),
                        unavailable: shading.nextAction({state: 'idle', supported: true, applied: false, methods: {h: {supported: true}, bffree: {supported: false}}}),
                        cancel: shading.nextAction({state: 'rebuilding', operation: 'revoke', applied: true, can_cancel: true}),
                        publishing: shading.nextAction({state: 'publishing', operation: 'revoke', applied: true, can_cancel: false}),
                      };
                      if (persistentActions.apply !== 'start' || persistentActions.revoke !== 'revoke' || persistentActions.unavailable !== null ||
                          persistentActions.cancel !== 'cancel' || persistentActions.publishing !== null)
                        throw new Error('Persistent Shading Correction actions are unavailable');
                      const progressCheck = window.Nd2ShadingCorrection.progress({
                        state: 'preparing_display', operation: 'apply', can_cancel: true, pct: 63, eta_seconds: 75});
                      const revokeProgress = window.Nd2ShadingCorrection.progress({
                        state: 'rebuilding', operation: 'revoke', applied: true, can_cancel: true, pct: 41, eta_seconds: 64});
                      if (!progressCheck.visible || progressCheck.pct !== 63 ||
                          progressCheck.stage !== 'Building corrected pyramid' || !progressCheck.canCancel ||
                          !progressCheck.eta.includes('remaining') || !revokeProgress.visible ||
                          revokeProgress.title !== 'Revoke Shading Correction' ||
                          revokeProgress.stage !== 'Rebuilding original pyramid' || !revokeProgress.canCancel ||
                          !revokeProgress.eta.includes('remaining'))
                        throw new Error('Shading Correction progress display is unavailable');
                      if (__CLIPBOARD_CHECK__) {
                        progress.phase = 'clipboard-transport';
                        const native = view.parent?.pywebview;
                        if (native?.platform !== 'edgechromium' ||
                            typeof native.api?.write_lut_clipboard !== 'function' ||
                            typeof native.api?.read_lut_clipboard !== 'function')
                          throw new Error('Native Windows LUT clipboard bridge is unavailable');
                        progress.clipboard_transport = 'native-winforms';
                      }
                      // Auto-LUT loading must settle before a same-values round trip.
                      if (__CLIPBOARD_CHECK__) await step('autolut', async () => {
                        const deadline = Date.now() + 15000;
                        while (view.eval('state.lutAutoPending.some(Boolean)')) {
                          if (Date.now() >= deadline) throw new Error('LUT smoke autolut timed out after 15000 ms');
                          await new Promise(resolve => setTimeout(resolve, 50));
                        }
                      }, 15000);
                      const assets = ['fluent-edit-20-regular.svg', 'fluent-copy-20-regular.svg', 'fluent-clipboard-paste-20-regular.svg',
                        'fluent-checkmark-20-regular.svg', 'fluent-circle-highlight-20-regular.svg'];
                      await step('icons', () => Promise.all(assets.map(async name => {
                        const response = await view.fetch(new URL('../../static/icons/' + name, page.baseURI));
                        if (!response.ok) throw new Error('LUT icon ' + name + ': HTTP ' + response.status);
                        const svg = new view.DOMParser().parseFromString(await response.text(), 'image/svg+xml');
                        if (svg.documentElement.tagName !== 'svg' || svg.documentElement.getAttribute('viewBox') !== '0 0 20 20' ||
                            !svg.querySelector('path')) throw new Error('Invalid packaged LUT icon: ' + name);
                      })));
                      const infoImages = await step('info-images', async () => {
                        const inspector = view.eval('state.windows.info');
                        const wasHidden = inspector.isHidden();
                        await view.loadSlideInspector();
                        inspector.open();
                        const names = view.eval('state.inspect && state.inspect.associated');
                        if (!Array.isArray(names)) throw new Error('Slide Info metadata did not load');
                        const images = [...page.querySelectorAll('#info-associated img')];
                        if (images.length !== names.length) throw new Error('Slide Info omitted embedded images');
                        const loaded = async () => {
                          const deadline = Date.now() + 8000;
                          while (images.some(image => !image.complete || !image.naturalWidth)) {
                            if (Date.now() >= deadline) throw new Error('Slide Info image did not load');
                            await new Promise(resolve => setTimeout(resolve, 40));
                          }
                        };
                        await loaded();
                        const dimensions = images.map(image => [image.naturalWidth, image.naturalHeight]);
                        let enlarged = null;
                        if (images.length) {
                          const image = images.find(item => !item.src.includes('/thumbnail.jpg')) || images[0];
                          const button = image.parentElement;
                          button.click();
                          await loaded();
                          if (button.getAttribute('aria-expanded') !== 'true' ||
                              !image.src.includes('detail=1') ||
                              Math.max(image.naturalWidth, image.naturalHeight) > 2048)
                            throw new Error('Slide Info enlargement is unavailable');
                          enlarged = [image.naturalWidth, image.naturalHeight];
                          button.click();
                        }
                        if (wasHidden) inspector.close();
                        return {names, dimensions, enlarged};
                      });
                      const outcome = {ok: true, phase: 'complete', module: true, handlers: true, icons: assets,
                        info_images: infoImages,
                        shading_correction: {state: shadingStatus.state, applied: shadingStatus.applied,
                          method: shadingStatus.method, methods: shadingStatus.methods,
                          operation: shadingStatus.operation, can_cancel: shadingStatus.can_cancel,
                          generation: shadingStatus.generation, actions: persistentActions,
                          progress: progressCheck, revoke_progress: revokeProgress, toolbar: true, confirmation: true},
                        clipboard_api: {write: typeof view.navigator.clipboard?.writeText === 'function',
                          read: typeof view.navigator.clipboard?.readText === 'function'}};
                      if (__CLIPBOARD_CHECK__) {
                        const current = () => view.eval('window.Nd2LutTransfer.encode(state.info.channels, state.luts, state.info.dtype)');
                        const before = current();
                        const expected = view.eval('window.Nd2LutTransfer.planPaste(window.Nd2LutTransfer.encode(state.info.channels, state.luts, state.info.dtype), state.info.channels, state.info.dtype).updates.length');
                        if (!expected) throw new Error('LUT clipboard fixture has no uniquely named compatible channels');
                        const [, copy, paste] = buttons;
                        const toast = () => page.getElementById('toast')?.textContent || 'No result reported';
                        await step('copy', () => copy.onclick());
                        if (copy.disabled || !copy.classList.contains('is-copied') || toast() !== 'Channel LUTs copied')
                          throw new Error('LUT clipboard copy failed: ' + toast());
                        await step('paste', () => paste.onclick());
                        if (paste.disabled || !toast().startsWith('Pasted ' + expected + ' channel LUT'))
                          throw new Error('LUT clipboard paste failed: ' + toast());
                        if (current() !== before) throw new Error('LUT clipboard round trip changed effective channel values');
                        outcome.clipboard_roundtrip = {ok: true, channels: expected, effective_values_unchanged: true,
                          transport: progress.clipboard_transport};
                      }
                      return outcome;
                    })().then(value => { frame.__nd2wsiSmokeLut = value; })
                      .catch(error => { frame.__nd2wsiSmokeLut = {...progress, pending: false, ok: false,
                        error: error.message || String(error)}; });
                  }
                  const handoff = __HANDOFF_CHECK__ && frame?.contentWindow.nd2HandoffApplied === true;
                  return {bridge: true, version: bridge.version,
                    phase: slide ? frame.__nd2wsiSmokeLut?.phase || 'lut-toolbar' : frame ? 'slide-render' : 'slide-load',
                    ...(__HANDOFF_CHECK__ ? {handoff_applied:handoff,
                      observed_display:handoff ? frame.contentWindow.nd2CaptureViewState() : null} : {}),
                    boot: !!document.getElementById('drop'),
                    slide, lut_toolbar: frame?.__nd2wsiSmokeLut || null};
                })()""".replace("__HANDOFF_CHECK__", "true" if expected is not None else "false")
                    .replace("__CLIPBOARD_CHECK__", "true" if clipboard_check else "false"))
                if result:
                    report["last_result"] = result
                    report["phase"] = result.get("phase", (result.get("lut_toolbar") or {}).get("phase", report["phase"]))
                if result and result.get("bridge") and result.get("slide" if expect_slide else "boot"):
                    if expect_slide and (result.get("lut_toolbar") or {}).get("ok") is not True:
                        if (result.get("lut_toolbar") or {}).get("ok") is False:
                            report.update(result)
                            report["error"] = result["lut_toolbar"]["error"]
                            break
                        time.sleep(0.05)
                        continue
                    if expected is not None:
                        from .view_state import matches_view_state

                        if not result.get("handoff_applied"):
                            time.sleep(0.05)
                            continue
                        report.update(result)
                        if not matches_view_state(expected, result.get("observed_display")):
                            raise ValueError("Actual browser display state differs from the requested handoff")
                    native_keyboard = run_keyboard(api, window, slide=bool(result.get("slide")))
                    report.update(result, native_keyboard=native_keyboard,
                                  ok=native_keyboard["status"] != "failed", phase="complete")
                    if native_keyboard["status"] == "failed":
                        report.update(phase="native-keyboard", error=native_keyboard.get("error") or
                                      native_keyboard.get("cleanup_error"))
                    elif expect_slide:
                        from .translucent_smoke import exercise_translucent_viewer

                        tracing = exercise_translucent_viewer(api, window)
                        report["translucent_viewer"] = tracing
                        if not tracing.get("ok"):
                            report.update(ok=False, phase="translucent-viewer",
                                          error=tracing.get("error", "Translucent viewer check failed"))
                    if report["ok"] and expect_slide and sys.platform == "win32":
                        from .window_smoke import exercise_independent_window

                        independent = exercise_independent_window(api)
                        report["independent_window"] = independent
                        if not independent.get("ok"):
                            report.update(ok=False, phase="independent-window",
                                          error=independent.get("error", "Independent window check failed"))
                    break
            except Exception as exc:
                report["last_error"] = str(exc)
            time.sleep(0.25)
        else:
            raise TimeoutError(f"GUI did not finish within {timeout:g} seconds (last phase: {report['phase']})")
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        # The GUI's main loop can return as soon as the window closes.
        _gui_smoke_close(api, window, report, timeout=close_timeout)
    return report
