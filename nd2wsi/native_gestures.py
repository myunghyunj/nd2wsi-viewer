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
import re
import sys
import threading
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


@dataclass(frozen=True)
class NativeGesturePhaseMasks:
    """AppKit phase bits used by the pure sequence state machine."""

    may_begin: int
    began: int
    ended: int
    cancelled: int

    @classmethod
    def from_appkit(cls, appkit: Any) -> NativeGesturePhaseMasks:
        return cls(
            int(appkit.NSEventPhaseMayBegin),
            int(appkit.NSEventPhaseBegan),
            int(appkit.NSEventPhaseEnded),
            int(appkit.NSEventPhaseCancelled),
        )


@dataclass(frozen=True)
class NativeSequenceDecision:
    """Routing result for one event in a physical-plus-momentum sequence."""

    target: str | None
    motion: HorizontalDecision
    hold_prefix: bool = False
    new_sequence: bool = False


class NativeGestureSequence:
    """Keep one axis and pane target through physical and momentum phases."""

    def __init__(
        self,
        phases: NativeGesturePhaseMasks,
        gate: HorizontalGestureGate | None = None,
    ) -> None:
        self.phases = phases
        self.gate = gate or HorizontalGestureGate()
        self.reset()

    def reset(self) -> None:
        self.gate.reset()
        self.state = "idle"
        self.target: str | None = None
        self.last_at_ms: float | None = None

    @staticmethod
    def _finite_time(value: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return 0.0
        return number if math.isfinite(number) else 0.0

    def _start(self, target: str | None, now_ms: float) -> None:
        self.gate.reset()
        self.state = "legacy"
        self.target = target
        self.last_at_ms = now_ms

    def feed(
        self,
        sample: NativeGestureSample,
        at_ms: float,
        candidate_target: str | None,
    ) -> NativeSequenceDecision:
        now = self._finite_time(at_ms)
        if self.last_at_ms is not None and now - self.last_at_ms > self.gate.idle_ms:
            self.reset()

        physical_started = bool(
            sample.phase & (self.phases.may_begin | self.phases.began)
        )
        physical_ended = bool(sample.phase & self.phases.ended)
        physical_cancelled = bool(sample.phase & self.phases.cancelled)
        momentum_finished = bool(
            sample.momentum_phase & (self.phases.ended | self.phases.cancelled)
        )

        new_sequence = False
        if sample.kind == "swipe":
            self._start(candidate_target, now)
            new_sequence = True
        elif physical_started and self.state != "physical":
            # MayBegin followed by Began is one sequence. A physical start after
            # Ended/awaiting-momentum, however, deliberately starts afresh.
            self._start(candidate_target, now)
            self.state = "physical"
            new_sequence = True
        elif self.state == "idle":
            # Covers phase-less mouse wheels and a monitor installed mid-stream.
            self._start(candidate_target, now)
            new_sequence = True

        self.last_at_ms = now
        target = self.target
        if target is None:
            motion = HorizontalDecision(False)
            hold_prefix = False
        else:
            motion = self.gate.feed(sample.delta_x, sample.delta_y, now)
            hold_prefix = (
                sample.kind == "scroll"
                and self.gate.axis is None
                and bool(sample.precise or sample.phase or sample.momentum_phase)
            )

        # Physical Ended is not a final boundary: AppKit may send momentum Began
        # in the same event or a following event. Keep both the axis and target
        # while waiting. The next physical start or the idle timeout cleans up a
        # gesture which has no momentum tail.
        if sample.kind == "swipe" or physical_cancelled or momentum_finished:
            self.reset()
        elif sample.momentum_phase:
            self.state = "momentum"
        elif physical_ended:
            self.state = "awaiting_momentum"
        elif sample.phase:
            self.state = "physical"

        return NativeSequenceDecision(target, motion, hold_prefix, new_sequence)


@dataclass(frozen=True)
class NormalizedRect:
    """A top-left-origin rectangle in normalized WKWebView coordinates."""

    left: float
    top: float
    right: float
    bottom: float

    def contains(self, x: float, y: float) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom


@dataclass(frozen=True)
class NativeGestureScope:
    """One pane's plate stage minus controls which keep native scrolling."""

    target: str
    include: NormalizedRect
    exclude: tuple[NormalizedRect, ...] = ()

    def accepts(self, x: float, y: float) -> bool:
        return self.include.contains(x, y) and not any(
            rect.contains(x, y) for rect in self.exclude
        )


class NativeGestureScopeCache:
    """Thread-safe geometry published asynchronously by the shell page."""

    MAX_SCOPES = 32
    MAX_EXCLUSIONS = 64
    _TARGET = re.compile(r"^[0-9a-f]{8}$")
    _TOKEN = re.compile(r"^[0-9a-f]{32}$")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: str | None = None
        self._revision = -1
        self._scopes: tuple[NativeGestureScope, ...] = ()

    def begin(self, token: str) -> bool:
        """Start one shell navigation and invalidate every older publisher."""

        if not isinstance(token, str) or self._TOKEN.fullmatch(token) is None:
            return False
        with self._lock:
            self._token = token
            self._revision = -1
            self._scopes = ()
        return True

    def clear(self) -> None:
        """Fail open while the top-level WKWebView navigates or closes."""

        with self._lock:
            self._token = None
            self._revision = -1
            self._scopes = ()

    @staticmethod
    def _rect(value: Any) -> NormalizedRect | None:
        if not isinstance(value, dict):
            return None
        numbers: list[float] = []
        for key in ("left", "top", "right", "bottom"):
            raw = value.get(key)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                return None
            try:
                number = float(raw)
            except (TypeError, ValueError, OverflowError):
                return None
            if not math.isfinite(number):
                return None
            if not 0.0 <= number <= 1.0:
                return None
            numbers.append(number)
        left, top, right, bottom = numbers
        if right <= left or bottom <= top:
            return None
        return NormalizedRect(left, top, right, bottom)

    def replace(self, payload: Any) -> dict[str, Any]:
        """Validate and atomically replace the browser-published snapshot."""

        if not isinstance(payload, dict):
            return {"ok": False, "error": "invalid payload"}
        token = payload.get("token")
        revision = payload.get("revision")
        if not isinstance(token, str) or self._TOKEN.fullmatch(token) is None:
            return {"ok": False, "error": "invalid token"}
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or not 0 <= revision <= 9_007_199_254_740_991
        ):
            return {"ok": False, "error": "invalid revision"}
        raw_scopes = payload.get("scopes")
        if not isinstance(raw_scopes, list) or len(raw_scopes) > self.MAX_SCOPES:
            return {"ok": False, "error": "invalid snapshot"}

        scopes: list[NativeGestureScope] = []
        seen: set[str] = set()
        valid = True
        for item in raw_scopes:
            if not isinstance(item, dict):
                valid = False
                break
            target = item.get("target")
            if not isinstance(target, str) or self._TARGET.fullmatch(target) is None:
                valid = False
                break
            if target in seen:
                valid = False
                break
            include = self._rect(item.get("include"))
            if include is None:
                valid = False
                break
            raw_exclude = item.get("exclude", [])
            if not isinstance(raw_exclude, list) or len(raw_exclude) > self.MAX_EXCLUSIONS:
                valid = False
                break
            parsed_exclude = [self._rect(value) for value in raw_exclude]
            if any(rect is None for rect in parsed_exclude):
                valid = False
                break
            exclude = tuple(rect for rect in parsed_exclude if rect is not None)
            scopes.append(NativeGestureScope(target, include, exclude))
            seen.add(target)

        with self._lock:
            if token != self._token:
                return {"ok": False, "error": "stale token"}
            if revision <= self._revision:
                return {"ok": True, "stale": True, "revision": self._revision}
            self._revision = revision
            self._scopes = tuple(scopes) if valid else ()
        if not valid:
            return {"ok": False, "error": "invalid scope", "revision": revision}
        return {"ok": True, "stale": False, "revision": revision}

    def target_at(self, x: float, y: float) -> str | None:
        try:
            px, py = float(x), float(y)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(px) or not math.isfinite(py):
            return None
        with self._lock:
            scopes = self._scopes
        for scope in scopes:
            if scope.accepts(px, py):
                return scope.target
        return None

    def has_target(self, target: str) -> bool:
        """Whether a captured pane is still eligible in the newest snapshot."""

        with self._lock:
            return any(scope.target == target for scope in self._scopes)

    def snapshot(self) -> tuple[str | None, int, tuple[NativeGestureScope, ...]]:
        """Return an immutable snapshot for tests and diagnostics."""

        with self._lock:
            return self._token, self._revision, self._scopes


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


def _dispatch_native_trackpad(
    native_webview: Any,
    payload: dict[str, Any],
    started: bool,
    logger: Callable[[str], None],
) -> None:
    script = (
        "window.nd2wsiNativeTrackpad && "
        f"window.nd2wsiNativeTrackpad({json.dumps(payload, separators=(',', ':'))})"
    )
    completion = None
    if started:

        def completion(result, error):
            if error is not None:
                logger(f"native trackpad JavaScript dispatch failed: {error!r}")
            else:
                logger(f"native trackpad JavaScript dispatch target={result!s}")

    native_webview.evaluateJavaScript_completionHandler_(script, completion)


def _make_native_trackpad_monitor(
    *,
    appkit: Any,
    native_window: Any,
    native_webview: Any,
    scope_cache: NativeGestureScopeCache,
    sequence: NativeGestureSequence,
    logger: Callable[[str], None],
    clock_ms: Callable[[], float] = lambda: time.monotonic() * 1000.0,
    dispatch: Callable[[dict[str, Any], bool], None] | None = None,
) -> Callable[[Any], Any]:
    """Build the local monitor with injectable native boundaries for tests."""

    last_diagnostic_at = -1_000_000.0
    send = dispatch or (
        lambda payload, started: _dispatch_native_trackpad(
            native_webview, payload, started, logger
        )
    )

    def monitor(event):
        nonlocal last_diagnostic_at
        try:
            sample = _native_event_sample(event, appkit)
            belongs, window_match = _event_belongs_to_window(event, native_window)
            if not belongs:
                return event
            try:
                event_window = event.window()
            except Exception:
                event_window = None
            if event_window is None:
                window_point = native_window.convertPointFromScreen_(
                    appkit.NSEvent.mouseLocation()
                )
            else:
                window_point = event.locationInWindow()
            point = native_webview.convertPoint_fromView_(window_point, None)
            bounds = native_webview.bounds()
            width = float(bounds.size.width)
            height = float(bounds.size.height)
            origin = getattr(bounds, "origin", None)
            origin_x = float(getattr(origin, "x", 0.0))
            origin_y = float(getattr(origin, "y", 0.0))
            local_x = float(point.x) - origin_x
            local_y = float(point.y) - origin_y
            inside = (
                math.isfinite(width)
                and math.isfinite(height)
                and math.isfinite(origin_x)
                and math.isfinite(origin_y)
                and math.isfinite(local_x)
                and math.isfinite(local_y)
                and width > 0.0
                and height > 0.0
                and 0.0 <= local_x < width
                and 0.0 <= local_y < height
            )
            if not inside:
                sequence.reset()
                return event

            client_x = local_x
            client_y = local_y
            if not bool(native_webview.isFlipped()):
                client_y = height - client_y
            x_ratio = client_x / width
            y_ratio = client_y / height
            candidate_target = scope_cache.target_at(x_ratio, y_ratio)
            now_ms = clock_ms()
            routed = sequence.feed(sample, now_ms, candidate_target)
            if routed.new_sequence and now_ms - last_diagnostic_at > 80.0:
                logger(
                    "native trackpad input "
                    f"kind={sample.kind} dx={sample.delta_x:.3f} "
                    f"dy={sample.delta_y:.3f} precise={int(sample.precise)} "
                    f"phase={sample.phase} momentum={sample.momentum_phase} "
                    f"window={window_match} target={routed.target or 'native'}"
                )
                last_diagnostic_at = now_ms

            if routed.target is None:
                return event
            if not scope_cache.has_target(routed.target):
                sequence.reset()
                return event
            if not routed.motion.consume:
                # Keep only an undecided precise prefix which began inside a
                # cached plate-stage scope. Once y wins, WebKit receives the
                # native sequence normally; outside the scope nothing is held.
                return None if routed.hold_prefix else event

            payload = {
                "target": routed.target,
                "deltaX": routed.motion.delta_x,
                "gestureStart": routed.motion.started,
                "clientX": client_x,
                "clientY": client_y,
                "clientXRatio": x_ratio,
                "clientYRatio": y_ratio,
                "altKey": bool(
                    int(_call_number(event, "modifierFlags"))
                    & appkit.NSEventModifierFlagOption
                ),
            }
            send(payload, routed.motion.started)
            if routed.motion.started:
                logger(
                    "native horizontal trackpad gesture "
                    f"kind={sample.kind} dx={routed.motion.delta_x:.3f} "
                    f"dy={sample.delta_y:.3f} "
                    f"at={client_x:.0f},{client_y:.0f} target={routed.target}"
                )
            return None
        except Exception as exc:
            logger(f"native trackpad bridge failed: {exc!r}")
            return event

    return monitor


_MONITORS: dict[str, tuple[Any, Any]] = {}
_SESSIONS: dict[str, NativeGestureSequence] = {}


def wire_native_trackpad_bridge(
    window: Any,
    logger: Callable[[str], None] = lambda _message: None,
    scope_cache: NativeGestureScopeCache | None = None,
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
    scopes = scope_cache or NativeGestureScopeCache()

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
            sequence = _SESSIONS.setdefault(
                key,
                NativeGestureSequence(NativeGesturePhaseMasks.from_appkit(AppKit)),
            )
            monitor = _make_native_trackpad_monitor(
                appkit=AppKit,
                native_window=native_window,
                native_webview=native_webview,
                scope_cache=scopes,
                sequence=sequence,
                logger=logger,
            )

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
        _SESSIONS.pop(key, None)
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
