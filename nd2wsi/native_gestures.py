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
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class HorizontalDecision:
    """One native scroll event's routing decision."""

    consume: bool
    delta_x: float = 0.0
    started: bool = False


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
        threshold: float = 3.0,
    ) -> None:
        self.idle_ms = max(1.0, float(idle_ms))
        self.dominance = max(1.0, float(dominance))
        self.threshold = max(0.1, float(threshold))
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
            gate = _GATES.setdefault(key, HorizontalGestureGate())

            def monitor(event):
                if event.window() != native_window:
                    return event
                try:
                    dx = float(event.scrollingDeltaX())
                    dy = float(event.scrollingDeltaY())
                    decision = gate.feed(dx, dy, time.monotonic() * 1000.0)
                    if not decision.consume:
                        return event
                    point = native_webview.convertPoint_fromView_(
                        event.locationInWindow(), None
                    )
                    client_y = float(point.y)
                    if not bool(native_webview.isFlipped()):
                        client_y = float(native_webview.bounds().size.height) - client_y
                    payload = json.dumps(
                        {
                            "deltaX": decision.delta_x,
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
                    native_webview.evaluateJavaScript_completionHandler_(script, None)
                    if decision.started:
                        logger(
                            "native horizontal trackpad gesture "
                            f"dx={decision.delta_x:.2f} dy={dy:.2f} "
                            f"at={float(point.x):.0f},{client_y:.0f}"
                        )
                    return None
                except Exception as exc:
                    logger(f"native trackpad bridge failed: {exc!r}")
                    return event

            token = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                AppKit.NSEventMaskScrollWheel,
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
