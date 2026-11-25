"""SLACS-specific app helpers: discovery/offer claims, translucent handoff, and WebView2 fallback.

Extracted from ``nd2wsi.app`` so ``nd2wsi/app.py`` remains a clean flat facade
preserving all public and test-referenced symbols and monkeypatch seams.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from urllib.parse import quote

from . import session_log, webview2_runtime
from .window_chrome import _direct_destroy_window as _direct_destroy_window

# -- SLACS discovery & offer claiming ------------------------------------------


def slacs_discovery_state(api, claim_offer: bool = False) -> dict:
    """Read-only detection for the main-window connection offer.

    ``offer`` is True only in the one viewer window of this role that first
    claimed the detected process, so the offer appears once across windows.
    The shell asks to claim only while the user is looking at that window.
    """
    from . import slacs_discovery

    with api._translucent_lock:
        if api._translucent_owner_closed:
            return {"available": False, "session_id": None, "status": "closed", "offer": False}
        if api._slacs_discovery is None:
            api._slacs_discovery = slacs_discovery.SlacsDiscovery(name="main")
        detector = api._slacs_discovery
    snapshot = detector.snapshot()
    log_slacs_detection(api, snapshot)
    snapshot["offer"] = claim_slacs_offer(api, snapshot, claim_offer is True)
    return snapshot


def log_slacs_detection(api, snapshot) -> None:
    """Record each detection change, so a failed detection can be diagnosed afterwards."""
    state = (snapshot.get("available") is True, snapshot.get("session_id"), snapshot.get("status"))
    with api._slacs_offer_lock:
        if state == api._slacs_logged:
            return
        api._slacs_logged = state
    session_log.event("slacs_detection", window="main", available=state[0], session_id=state[1],
                      status=state[2])


def claim_slacs_offer(api, snapshot, claim: bool) -> bool:
    """Claim the once-per-process SLACS offer for this window role."""
    from . import slacs_discovery

    session = snapshot.get("session_id")
    if snapshot.get("available") is not True or snapshot.get("status") != "running" or not isinstance(session, str):
        return False
    with api._slacs_offer_lock:
        if session not in api._slacs_offers:
            if not claim:
                return False  # a background window neither claims nor gives up the offer
            if api._window_session is None:
                api._slacs_offers[session] = True  # no shared window identity: this is the only window
            else:
                if api._slacs_offer_claims is None:
                    api._slacs_offer_claims = slacs_discovery.SlacsOfferClaims(
                        api._window_session.root.parent / "slacs-offers" / api._window_session.role,
                        claimant=api._window_session.id,
                    )
                api._slacs_offers[session] = api._slacs_offer_claims.claim(session)
        return api._slacs_offers[session]


# -- Translucent viewer handoff & lifecycle ------------------------------------


def follow_flip_store(api):
    """This window process's store of Follow flips per image format.

    An Agent window's store keeps a private copy under its session root.
    """
    from .follow_flips import FollowFlipStore

    with api._translucent_lock:
        if api._follow_flips_store is None:
            api._follow_flips_store = FollowFlipStore(api._window_session)
        return api._follow_flips_store


def _overlay_is_closed(overlay) -> bool:
    if callable(getattr(overlay, "_is_closed", None)):
        return bool(overlay._is_closed())
    if callable(getattr(overlay, "_session_snapshot", None)):
        return bool(overlay._session_snapshot()[3])
    return bool(getattr(overlay, "_closed", False))


def prepare_translucent_context(api, sid: str, payload: dict, *, server_url) -> tuple[str, dict, str]:
    """Validate and assemble the slide handoff context for a translucent tracing window."""
    from .translucent_window import validate_context

    with api._server_lock:
        server = api._httpd
        store = server.registry.get(sid) if server is not None and isinstance(sid, str) else None
        if store is None:
            raise ValueError("Open an image before opening the translucent viewer")
        launch_generation = getattr(store, "generation", "")
        base_url = server_url(server).rstrip("/") + "/"
        context = validate_context(store, payload, slide_url=base_url + "s/" + quote(sid, safe="") + "/")
    return base_url, context, launch_generation


def acquire_translucent_slot(api, sid: str):
    """Check for an existing overlay or acquire the exclusive tracing claim."""
    from .translucent_claim import tracing_claim

    with api._translucent_lock:
        if api._translucent_owner_closed:
            raise ValueError("The source viewer is closing")
        previous = api._translucent_windows.get(sid)
        if previous is not None and not _overlay_is_closed(previous):
            return previous, None, None
        if api._translucent_windows or api._translucent_claim is not None:
            session_log.event("translucent_refused", reason="another_open", sid=sid)
            return None, None, {
                "ok": False,
                "reason": "another_open",
                "message": "A tracing window is already open. Close it before opening another.",
            }
        claim = tracing_claim(api._window_session)
        try:
            claim.acquire()
        except TimeoutError:
            session_log.event("translucent_refused", reason="another_open", sid=sid)
            return None, None, {
                "ok": False,
                "reason": "another_open",
                "message": "A tracing window is already open. Close it before opening another.",
            }
        api._translucent_claim = claim
        return None, claim, None


def prepare_and_acquire_translucent_slot(
    api,
    sid: str,
    payload: dict,
    *,
    launch_token: str,
    server_url,
):
    """Atomically resolve store, validate context, reserve launch_token, and acquire claim."""
    from .translucent_claim import tracing_claim
    from .translucent_window import validate_context

    with api._server_lock:
        with api._translucent_lock:
            server = api._httpd
            store = server.registry.get(sid) if server is not None and isinstance(sid, str) else None
            if store is None:
                raise ValueError("Open an image before opening the translucent viewer")
            launch_generation = getattr(store, "generation", "")
            base_url = server_url(server).rstrip("/") + "/"
            context = validate_context(
                store,
                payload,
                slide_url=base_url + "s/" + quote(sid, safe="") + "/",
            )
            if api._translucent_owner_closed:
                raise ValueError("The source viewer is closing")
            previous = api._translucent_windows.get(sid)
            if previous is not None and not _overlay_is_closed(previous):
                api._translucent_pending_launches[sid] = launch_token
                return base_url, context, launch_generation, previous, None, None
            if api._translucent_windows or api._translucent_claim is not None:
                session_log.event("translucent_refused", reason="another_open", sid=sid)
                return (
                    base_url,
                    context,
                    launch_generation,
                    None,
                    None,
                    {
                        "ok": False,
                        "reason": "another_open",
                        "message": "A tracing window is already open. Close it before opening another.",
                    },
                )
            claim = tracing_claim(api._window_session)
            try:
                claim.acquire()
            except TimeoutError:
                session_log.event("translucent_refused", reason="another_open", sid=sid)
                return (
                    base_url,
                    context,
                    launch_generation,
                    None,
                    None,
                    {
                        "ok": False,
                        "reason": "another_open",
                        "message": "A tracing window is already open. Close it before opening another.",
                    },
                )
            api._translucent_claim = claim
            api._translucent_pending_launches[sid] = launch_token
            return base_url, context, launch_generation, None, claim, None


def cancel_pending_translucent_launch(api, sid: str, launch_token: str) -> None:
    """Clear a pending launch reservation if it still belongs to ``launch_token``."""
    if not isinstance(sid, str):
        return
    with api._translucent_lock:
        if api._translucent_pending_launches.get(sid) == launch_token:
            api._translucent_pending_launches.pop(sid, None)


def reopen_translucent_overlay(
    api,
    sid: str,
    previous,
    context: dict,
    *,
    launch_token: str,
    launch_generation: str,
) -> dict:
    """Serialize and validate same-window reopen without disturbing superseded sessions."""
    refresh_lock = getattr(previous, "_refresh_lock", None)
    lock_ctx = refresh_lock if refresh_lock is not None else contextlib.nullcontext()
    with lock_ctx:
        def _verify(*, pop_pending: bool) -> None:
            with api._server_lock:
                with api._translucent_lock:
                    server = api._httpd
                    current_store = server.registry.get(sid) if server is not None else None
                    pending_matches = api._translucent_pending_launches.get(sid) == launch_token
                    same_overlay = api._translucent_windows.get(sid) is previous
                    closed = _overlay_is_closed(previous)
                    configured = bool(getattr(previous, "_configured", True))
                    if api._translucent_owner_closed:
                        if pending_matches:
                            api._translucent_pending_launches.pop(sid, None)
                        raise ValueError("The source viewer is closing")
                    if current_store is None:
                        if pending_matches:
                            api._translucent_pending_launches.pop(sid, None)
                        raise ValueError("The slide was closed while opening the overlay")
                    if not pending_matches:
                        raise ValueError("The overlay launch was superseded")
                    if getattr(current_store, "generation", "") != launch_generation:
                        api._translucent_pending_launches.pop(sid, None)
                        raise ValueError("The image changed; refresh the slide before opening the overlay")
                    if not same_overlay or closed or not configured:
                        api._translucent_pending_launches.pop(sid, None)
                        raise ValueError("The tracing window is still opening or closing.")
                    if pop_pending:
                        api._translucent_pending_launches.pop(sid, None)

        _verify(pop_pending=False)
        if callable(getattr(previous, "stop_stage_follow", None)):
            previous.stop_stage_follow()
            _verify(pop_pending=True)
            previous._refresh_context(
                context,
                launch_token=launch_token,
                launch_generation=launch_generation,
                _skip_stop=True,
            )
        else:
            _verify(pop_pending=True)
            previous._refresh_context(
                context,
                launch_token=launch_token,
                launch_generation=launch_generation,
            )
    return {"ok": True, "id": previous._window.uid, "launch_token": launch_token}


def launch_translucent_overlay(
    api,
    sid: str,
    context: dict,
    *,
    base_url: str,
    claim,
    app_name: str,
    logger,
    launch_token: str | None = None,
    launch_generation: str = "",
):
    """Create the native translucent window and wire its close/lock/laser-box callbacks."""
    from .laser_target import LaserTargetStore
    from .translucent_window import create_translucent_window

    def remove(closed):
        with api._translucent_lock:
            if api._translucent_windows.get(sid) is closed:
                api._translucent_windows.pop(sid, None)
            if api._translucent_claim is claim:
                api._translucent_claim = None
            claim.release()
        session_log.event("translucent_closed", sid=sid)

    def active(locked):
        if locked:
            api._active_translucent_sid = sid

    def deliver(message):
        # Called only by the overlay's bounded delivery worker, without either lock.
        owner = api._lut_clipboard_owner()
        return owner.evaluate_js(
            "window.nd2wsiSlacsLaserBox?.("
            + json.dumps({**message, "sid": sid}, allow_nan=False)
            + ") === true"
        )

    return create_translucent_window(
        context,
        url=base_url + "static/translucent.html",
        title=app_name + " — Translucent",
        logger=logger,
        on_closed=remove,
        flip_store=api._follow_flip_store(),
        target_store=LaserTargetStore(api._window_session),
        on_laser_box=deliver,
        is_active=lambda: api._active_translucent_sid == sid,
        on_lock=active,
        launch_token=launch_token,
        launch_generation=launch_generation,
    )


def register_translucent_overlay(
    api,
    sid: str,
    overlay,
    *,
    launch_token: str | None = None,
    launch_generation: str = "",
) -> dict:
    """Register the created overlay unless aborted by close, removal, expiry, or superseding launch."""
    token = launch_token if isinstance(launch_token, str) and launch_token else uuid.uuid4().hex
    abort_reason = None
    with api._server_lock:
        with api._translucent_lock:
            pending_matches = (
                launch_token is None or api._translucent_pending_launches.get(sid) == launch_token
            )
            if launch_token is not None and pending_matches:
                api._translucent_pending_launches.pop(sid, None)
            server = api._httpd
            current_store = server.registry.get(sid) if server is not None else None
            overlay_closed = _overlay_is_closed(overlay)
            if api._translucent_owner_closed:
                abort_reason = "The source viewer is closing"
            elif current_store is None:
                abort_reason = "The slide was closed while opening the overlay"
            elif not pending_matches:
                abort_reason = "The overlay launch was superseded"
            elif getattr(current_store, "generation", "") != launch_generation:
                abort_reason = "The image changed; refresh the slide before opening the overlay"
            elif overlay_closed:
                abort_reason = "The overlay closed while opening"
            else:
                api._translucent_windows[sid] = overlay
    if abort_reason is not None:
        overlay.close_translucent()
        raise ValueError(abort_reason)
    session_log.event("translucent_opened", sid=sid, id=overlay._window.uid)
    return {"ok": True, "id": overlay._window.uid, "launch_token": token}


def release_translucent_claim(api, claim) -> None:
    """Release an unowned tracing claim if overlay creation failed before handoff."""
    with api._translucent_lock:
        if api._translucent_claim is claim:
            api._translucent_claim = None
        claim.release()


def handle_translucent_open_error(sid, exc: Exception, *, logger) -> dict:
    logger(f"translucent viewer open failed: {exc!r}")
    session_log.event("translucent_open_failed", sid=sid, ok=False, error=str(exc))
    return {"ok": False, "message": f"Could not open translucent viewer: {exc}"}


def report_slacs_laser_box(api, payload) -> dict:
    """Forward a main-pane laser-box acknowledgement back to the matching translucent overlay."""
    if not isinstance(payload, dict) or not isinstance(payload.get("sid"), str):
        return {"ok": False}
    with api._translucent_lock:
        overlay = api._translucent_windows.get(payload["sid"])
    return overlay._report_slacs_laser_box(payload) if overlay is not None else {"ok": False}


def close_translucent_viewer(api, sid: str, *, logger) -> dict:
    """Close the translucent tracing window for one slide tab, if open."""
    with api._translucent_lock:
        if isinstance(sid, str):
            overlay = api._translucent_windows.pop(sid, None)
            pending = api._translucent_pending_launches.pop(sid, None)
        else:
            overlay = None
            pending = None
    if overlay is not None:
        try:
            overlay.close_translucent()
        except Exception as exc:
            logger(f"translucent viewer close failed: {exc!r}")
            return {"ok": False, "message": str(exc)}
    return {"ok": True, "closed": overlay is not None or pending is not None}


def close_translucent_windows(api, *, owner_closing: bool = True, logger) -> None:
    """Close all translucent overlays and stop main-window SLACS discovery."""
    with api._translucent_lock:
        if owner_closing:
            api._translucent_owner_closed = True
        overlays = list(api._translucent_windows.values())
        api._translucent_windows.clear()
        api._translucent_pending_launches.clear()
        detector, api._slacs_discovery = api._slacs_discovery, None
    if overlays:
        session_log.event("translucent_closing", count=len(overlays), reason="source viewer closing")
    if detector is not None:
        try:
            detector.stop()
        except Exception as exc:
            logger(f"SLACS discovery close failed: {exc!r}")
    for overlay in overlays:
        try:
            overlay.close_translucent()
        except Exception as exc:
            logger(f"translucent viewer close failed: {exc!r}")
    # The native closed callback releases the claim. Keep it held if destruction
    # fails, until the OS reclaims it at process exit.


# -- Windows WebView2 startup, dismissal & 1-shot auto fallback ----------------

_DISMISS_SHOWN_TIMEOUT = 1.5


def dismiss_failed_webview2_window(
    api,
    window,
    *,
    spawn=None,
    shown_timeout: float = _DISMISS_SHOWN_TIMEOUT,
) -> None:
    """Close a window whose WebView2 never started, so its error or fallback runs at once.

    The page never loaded, so the window holds no unsaved work and the close is
    approved without the save handshake. Runs on its own daemon thread, not inside
    the WebView2 completion callback on the UI thread. If ``window.events.shown``
    never fires within ``shown_timeout`` seconds (or ``window.destroy()`` raises),
    falls back to direct native/GUI teardown so the blank window never hangs.
    """
    coordinator = getattr(api, "_close_coordinator", None)
    if coordinator is not None:
        coordinator.approve_close("WebView2 failed to start, so the page never loaded")

    def _try_direct_destroy() -> bool:
        try:
            return bool(_direct_destroy_window(window))
        except Exception:
            return False

    def close_window() -> None:
        shown = getattr(getattr(window, "events", None), "shown", None)
        has_waitable_shown = callable(getattr(shown, "is_set", None)) and callable(getattr(shown, "wait", None))
        if has_waitable_shown:
            if shown.is_set() or shown.wait(shown_timeout):
                try:
                    window.destroy()
                    return
                except Exception:
                    _try_direct_destroy()
                    return
            # ``shown`` never fired: bypass pywebview's 20 s @_shown_call wait.
            if not _try_direct_destroy():
                try:
                    window.destroy()
                except Exception:
                    pass
            return
        try:
            window.destroy()
        except Exception:
            _try_direct_destroy()

    (spawn or threading.Thread)(target=close_window, name="webview2-failed-close", daemon=True).start()


def configure_win32_webview2(api, window, webview, *, diagnostic: bool, logger, dismiss) -> None:
    """Select the Windows WebView2 runtime, grant Win10 AppContainer ACLs, and watch init."""
    # pywebview reads the runtime folder when webview.start imports its
    # WinForms backend, so choose before any start. A forced bundled
    # copy that is missing fails startup here, never silently falls back.
    api._webview2_runtime = webview2_runtime.select_runtime(webview).report()
    if api._webview2_runtime.get("kind") == "bundled" and api._webview2_runtime.get("path"):
        # Windows 10 only (or a CI diagnostic): the sandboxed WebView2
        # processes need read+execute access to the bundled runtime folder.
        api._webview2_runtime["appcontainer_access"] = webview2_runtime.ensure_appcontainer_access(
            Path(api._webview2_runtime["path"]),
            force=os.environ.get(webview2_runtime.GRANT_SMOKE_ENV) == "1",
        )
    logger(f"WebView2 runtime: {api._webview2_runtime}")
    session_log.event("webview2_runtime", selection=api._webview2_runtime)

    def webview2_started(ok, detail, *, diagnostic=diagnostic):
        api._webview2_init = {"ok": ok, "error": detail}
        session_log.event("webview2_init", ok=ok, error=detail)
        if not ok:
            logger(f"WebView2 initialization failed: {detail}")
            if not diagnostic and not api._startup_error:
                # Shown after the blank window closes (unless 1-shot fallback succeeds);
                # the GUI smoke reports the failure itself.
                runtime = api._webview2_runtime or {}
                access = str(runtime.get("appcontainer_access") or "")
                hint = f" (folder permission step {access})" if access.startswith("failed") else ""
                api._startup_error = (
                    f"Microsoft Edge WebView2 ({runtime.get('kind', 'system')} runtime) "
                    f"could not start: {detail}{hint}"
                )
            if not diagnostic:
                dismiss(api, window)

    # Hook BEFORE webview.start / before_show so synchronous init failures are never missed.
    webview2_runtime.watch_initialization(window, webview2_started)


def build_relaunch_command(
    args,
    *,
    raw_argv: list[str] | None = None,
    executable: str | None = None,
    frozen: bool | None = None,
) -> list[str]:
    """Command line to relaunch this window once with the alternate WebView2 runtime."""
    exe = executable or sys.executable
    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else bool(frozen)
    if raw_argv is not None:
        forwarded = list(raw_argv)
    else:
        forwarded = []
        if getattr(args, "agent_window", False):
            forwarded.append("--agent-window")
        elif getattr(args, "new_window", False):
            forwarded.append("--new-window")
        if getattr(args, "nd2", None):
            forwarded.append(str(args.nd2))
    if is_frozen:
        return [exe, *forwarded]
    return [exe, "-m", "nd2wsi.app", *forwarded]


def maybe_relaunch_webview2_fallback(
    api,
    args,
    *,
    raw_argv: list[str] | None = None,
    environ=None,
    app_root=None,
    registry=None,
    verify_binary: bool | None = None,
    binary_exists=None,
    popen=None,
    logger=None,
) -> bool:
    """In Windows ``auto`` mode, relaunch once with the alternate runtime after init failure.

    Returns True when a 1-shot fallback relaunch was spawned (caller should suppress
    the startup error dialog and exit cleanly); returns False otherwise.
    """
    if sys.platform != "win32":
        return False
    if getattr(args, "gui_smoke", False) or getattr(args, "browser_replay_report", None):
        return False
    init_state = getattr(api, "_webview2_init", None)
    if not isinstance(init_state, dict) or init_state.get("ok") is not False:
        return False
    selection = getattr(api, "_webview2_runtime", None)
    target_mode = webview2_runtime.fallback_runtime_mode(
        selection,
        environ=environ,
        app_root=app_root,
        registry=registry,
        verify_binary=verify_binary,
        binary_exists=binary_exists,
    )
    if target_mode is None:
        return False

    command = build_relaunch_command(args, raw_argv=raw_argv)
    child_env = dict(os.environ if environ is None else environ)
    child_env[webview2_runtime.MODE_ENV] = target_mode
    child_env[webview2_runtime.FALLBACK_TRIED_ENV] = "1"
    child_env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    spawn_kwargs: dict = {
        "env": child_env,
        "close_fds": True,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    detached = getattr(subprocess, "DETACHED_PROCESS", 0)
    new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    if detached or new_group:
        spawn_kwargs["creationflags"] = detached | new_group

    from_kind = selection.get("kind") if isinstance(selection, dict) else None
    try:
        (popen or subprocess.Popen)(command, **spawn_kwargs)
    except OSError as exc:
        if logger is not None:
            logger(f"WebView2 1-shot fallback relaunch ({from_kind} -> {target_mode}) failed: {exc!r}")
        session_log.event(
            "webview2_fallback_relaunch",
            ok=False,
            from_kind=from_kind,
            to_mode=target_mode,
            error=repr(exc),
        )
        return False

    if logger is not None:
        logger(f"WebView2 init failed on {from_kind} runtime; relaunching once with {target_mode} runtime")
    session_log.event(
        "webview2_fallback_relaunch",
        ok=True,
        from_kind=from_kind,
        to_mode=target_mode,
        init_error=init_state.get("error"),
    )
    return True


def startup_error(message: str, *, app_name: str, show_dialog: bool = True, logger) -> None:
    logger(message)
    session_log.event("error", where="startup", message=message)
    print(message, file=sys.stderr)
    if sys.platform == "win32" and show_dialog:
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, app_name, 0x10)
        except Exception:
            pass


__all__ = [
    "acquire_translucent_slot",
    "build_relaunch_command",
    "cancel_pending_translucent_launch",
    "claim_slacs_offer",
    "close_translucent_viewer",
    "close_translucent_windows",
    "configure_win32_webview2",
    "dismiss_failed_webview2_window",
    "follow_flip_store",
    "handle_translucent_open_error",
    "launch_translucent_overlay",
    "log_slacs_detection",
    "maybe_relaunch_webview2_fallback",
    "prepare_and_acquire_translucent_slot",
    "prepare_translucent_context",
    "register_translucent_overlay",
    "release_translucent_claim",
    "reopen_translucent_overlay",
    "report_slacs_laser_box",
    "slacs_discovery_state",
    "startup_error",
]
