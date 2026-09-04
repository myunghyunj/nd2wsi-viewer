"""macOS trackpad delivery for the packaged WKWebView.

WKWebView may consume horizontal ``scrollWheel:`` events in its native
``NSScrollView`` without creating a DOM ``wheel`` event when the document has
no horizontally scrollable area.  Plate navigation deliberately uses that
otherwise-unused axis for time, so the packaged app forwards only gestures
that have become clearly horizontal.  Vertical gestures continue through
WebKit unchanged.
"""

from __future__ import annotations

import json
import math
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HorizontalDecision:
    """One native scroll event's routing decision."""

    consume: bool
    delta_x: float = 0.0
    started: bool = False


@dataclass(frozen=True)
class NativeGestureSample:
    """Normalized motion from an AppKit scroll or swipe event."""

    kind: str
    delta_x: float
    delta_y: float
    phase: int = 0
    momentum_phase: int = 0
    precise: bool = False


class HorizontalGestureGate:
    """Latch a native scroll sequence to one axis until it goes idle.

    Events before a decision are left to WKWebView.  When horizontal motion
    wins, the first forwarded delta includes that undecided prefix so a short
    physical swipe still produces a time step.  When vertical motion wins,
    every event remains native and the existing z/zoom behavior is untouched.
    """

    def __init__(
        self,
        *,
        idle_ms: float = 100.0,
        dominance: float = 1.2,
        threshold: float = 0.05,
    ) -> None:
        self.idle_ms = max(1.0, float(idle_ms))
        self.dominance = max(1.0, float(dominance))
        self.threshold = max(0.01, float(threshold))
        self.reset()

    def reset(self) -> None:
        self.axis: str | None = None
        self.sum_x = 0.0
        self.sum_y = 0.0
        self.last_at_ms: float | None = None

    @staticmethod
    def _finite(value: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0
        return number if math.isfinite(number) else 0.0

    def feed(self, delta_x: float, delta_y: float, at_ms: float) -> HorizontalDecision:
        now = self._finite(at_ms)
        if self.last_at_ms is not None and now - self.last_at_ms > self.idle_ms:
            self.reset()
        self.last_at_ms = now
        dx = self._finite(delta_x)
        dy = self._finite(delta_y)

        if self.axis == "x":
            return HorizontalDecision(True, dx)
        if self.axis == "y":
            return HorizontalDecision(False)

        self.sum_x += dx
        self.sum_y += dy
        ax = abs(self.sum_x)
        ay = abs(self.sum_y)
        if max(ax, ay) < self.threshold:
            return HorizontalDecision(False)
        if ax >= ay * self.dominance:
            self.axis = "x"
            return HorizontalDecision(True, self.sum_x, True)
        if ay >= ax * self.dominance:
            self.axis = "y"
        return HorizontalDecision(False)


def _call_number(subject: Any, name: str, default: float = 0.0) -> float:
    """Read a numeric Objective-C selector without trusting its result."""

    try:
        value = getattr(subject, name)()
    except Exception:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _native_event_sample(event: Any, appkit: Any) -> NativeGestureSample:
    """Return deltas that work for precise trackpads and legacy/swipe events."""

    event_type = int(_call_number(event, "type", -1))
    phase = int(_call_number(event, "phase"))
    momentum_phase = int(_call_number(event, "momentumPhase"))
    if event_type == int(appkit.NSEventTypeSwipe):
        return NativeGestureSample(
            "swipe",
            _call_number(event, "deltaX"),
            _call_number(event, "deltaY"),
            phase,
            momentum_phase,
            False,
        )

    try:
        precise = bool(event.hasPreciseScrollingDeltas())
    except Exception:
        precise = False
    scroll_x = _call_number(event, "scrollingDeltaX")
    scroll_y = _call_number(event, "scrollingDeltaY")
    legacy_x = _call_number(event, "deltaX")
    legacy_y = _call_number(event, "deltaY")
    # A physical trackpad normally supplies precise scrolling deltas. Some
    # AppKit/WebKit paths expose only the older delta selectors, however;
    # synthetic CGEvents happened to populate the former and hid this case.
    if precise or scroll_x or scroll_y:
        dx, dy = scroll_x, scroll_y
        if not dx and not dy and (legacy_x or legacy_y):
            dx, dy = legacy_x, legacy_y
    else:
        dx, dy = legacy_x, legacy_y
    return NativeGestureSample("scroll", dx, dy, phase, momentum_phase, precise)


def _event_belongs_to_window(event: Any, native_window: Any) -> tuple[bool, str]:
    """Match an event to the host window, including nil-window trackpad input."""

    try:
        event_window = event.window()
    except Exception:
        event_window = None
    if event_window is native_window:
        return True, "same"
    if event_window is not None:
        try:
            if int(event_window.windowNumber()) == int(native_window.windowNumber()):
                return True, "number"
        except Exception:
            pass
        return False, "other"
    # Local monitors only see this application's events. AppKit can deliver a
    # real gesture with no window even while the app's key window is the
    # destination, whereas our synthetic verification always had a window.
    try:
        active = bool(native_window.isKeyWindow()) or bool(native_window.isMainWindow())
    except Exception:
        active = False
    return active, "nil-key" if active else "nil-inactive"


_MONITORS: dict[str, tuple[Any, Any]] = {}
_GATES: dict[str, HorizontalGestureGate] = {}


def wire_native_trackpad_bridge(
    window: Any,
    logger: Callable[[str], None] = lambda _message: None,
) -> bool:
    """Forward horizontal scroll events before WKWebView can discard them.

    A process-local NSEvent monitor runs before AppKit sends the event to the
    deepest private WKWebView scroll subview. It consumes only a gesture that
    has latched horizontally for this window; all undecided and vertical
    events are returned to AppKit unchanged.
    """

    if sys.platform != "darwin":
        return False
    key = str(window.uid)

    def install() -> None:
        if key in _MONITORS:
            return
        try:
            import AppKit
            from webview.platforms.cocoa import BrowserView

            browser = BrowserView.instances.get(window.uid)
            if browser is None or browser.webview is None:
                raise RuntimeError("pywebview Cocoa host is not ready")
            native_window = browser.window
            native_webview = browser.webview
            try:
                native_webview.setAllowsBackForwardNavigationGestures_(False)
            except Exception:
                pass
            gate = _GATES.setdefault(key, HorizontalGestureGate())
            last_diagnostic_at = -1_000_000.0

            def monitor(event):
                nonlocal last_diagnostic_at
                try:
                    sample = _native_event_sample(event, AppKit)
                    belongs, window_match = _event_belongs_to_window(
                        event, native_window
                    )
                    if not belongs:
                        return event
                    try:
                        event_window = event.window()
                    except Exception:
                        event_window = None
                    if event_window is None:
                        window_point = native_window.convertPointFromScreen_(
                            AppKit.NSEvent.mouseLocation()
                        )
                    else:
                        window_point = event.locationInWindow()
                    point = native_webview.convertPoint_fromView_(window_point, None)
                    bounds = native_webview.bounds()
                    width = float(bounds.size.width)
                    height = float(bounds.size.height)
                    inside = (
                        0.0 <= float(point.x) <= width
                        and 0.0 <= float(point.y) <= height
                    )
                    if not inside:
                        gate.reset()
                        return event

                    now_ms = time.monotonic() * 1000.0
                    starts = bool(
                        sample.phase
                        & (AppKit.NSEventPhaseMayBegin | AppKit.NSEventPhaseBegan)
                    ) or bool(sample.momentum_phase & AppKit.NSEventPhaseBegan)
                    ends = bool(
                        sample.phase
                        & (AppKit.NSEventPhaseEnded | AppKit.NSEventPhaseCancelled)
                    ) or bool(
                        sample.momentum_phase
                        & (AppKit.NSEventPhaseEnded | AppKit.NSEventPhaseCancelled)
                    )
                    new_sequence = (
                        starts
                        or gate.last_at_ms is None
                        or now_ms - gate.last_at_ms > gate.idle_ms
                    )
                    if starts:
                        gate.reset()
                    if new_sequence and now_ms - last_diagnostic_at > 80.0:
                        logger(
                            "native trackpad input "
                            f"kind={sample.kind} dx={sample.delta_x:.3f} "
                            f"dy={sample.delta_y:.3f} precise={int(sample.precise)} "
                            f"phase={sample.phase} momentum={sample.momentum_phase} "
                            f"window={window_match}"
                        )
                        last_diagnostic_at = now_ms

                    decision = gate.feed(sample.delta_x, sample.delta_y, now_ms)
                    if not decision.consume:
                        # A physical scroll begins with MayBegin/Began and tiny
                        # deltas. If that undecided prefix reaches WKWebView,
                        # its NSScrollView may enter a nested tracking loop and
                        # the local monitor never sees the later horizontal
                        # samples. Hold only the undecided physical prefix;
                        # once y wins, vertical input goes back to WebKit.
                        physical_prefix = (
                            sample.kind == "scroll"
                            and gate.axis is None
                            and bool(
                                sample.precise
                                or sample.phase
                                or sample.momentum_phase
                            )
                        )
                        if ends:
                            gate.reset()
                        if physical_prefix:
                            return None
                        return event
                    client_y = float(point.y)
                    if not bool(native_webview.isFlipped()):
                        client_y = height - client_y
                    payload = json.dumps(
                        {
                            "deltaX": decision.delta_x,
                            "gestureStart": decision.started,
                            "clientX": float(point.x),
                            "clientY": client_y,
                            "altKey": bool(
                                int(event.modifierFlags())
                                & AppKit.NSEventModifierFlagOption
                            ),
                        },
                        separators=(",", ":"),
                    )
                    script = (
                        "window.nd2wsiNativeTrackpad && "
                        f"window.nd2wsiNativeTrackpad({payload})"
                    )
                    completion = None
                    if decision.started:

                        def completion(result, error):
                            if error is not None:
                                logger(
                                    "native trackpad JavaScript dispatch failed: "
                                    f"{error!r}"
                                )
                            else:
                                logger(
                                    "native trackpad JavaScript dispatch "
                                    f"target={result!s}"
                                )

                    native_webview.evaluateJavaScript_completionHandler_(
                        script, completion
                    )
                    if decision.started:
                        logger(
                            "native horizontal trackpad gesture "
                            f"kind={sample.kind} dx={decision.delta_x:.3f} "
                            f"dy={sample.delta_y:.3f} "
                            f"at={float(point.x):.0f},{client_y:.0f}"
                        )
                    if ends or sample.kind == "swipe":
                        gate.reset()
                    return None
                except Exception as exc:
                    logger(f"native trackpad bridge failed: {exc!r}")
                    return event

            token = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                AppKit.NSEventMaskScrollWheel | AppKit.NSEventMaskSwipe,
                monitor,
            )
            _MONITORS[key] = (token, monitor)
            logger("native trackpad bridge installed")
        except Exception as exc:
            logger(
                f"native trackpad bridge unavailable: {type(exc).__name__}: {exc}"
            )

    def schedule_install(*_args) -> None:
        try:
            from Foundation import NSOperationQueue

            NSOperationQueue.mainQueue().addOperationWithBlock_(install)
        except Exception:
            install()

    def remove(*_args) -> None:
        pair = _MONITORS.pop(key, None)
        _GATES.pop(key, None)
        if pair is None:
            return
        try:
            import AppKit

            AppKit.NSEvent.removeMonitor_(pair[0])
        except Exception as exc:
            logger(f"native trackpad bridge removal failed: {exc!r}")

    window.events.shown += schedule_install
    window.events.closed += remove
    return True
