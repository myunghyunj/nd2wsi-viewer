"""Native macOS gesture routing and its JavaScript hand-off."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from nd2wsi.native_gestures import (
    HorizontalDecision,
    HorizontalGestureGate,
    NativeGesturePhaseMasks,
    NativeGestureSample,
    NativeGestureScopeCache,
    NativeGestureSequence,
    _event_belongs_to_window,
    _make_native_trackpad_monitor,
    _native_event_sample,
)

ROOT = Path(__file__).resolve().parents[1]
SCOPE_TOKEN = "0123456789abcdef0123456789abcdef"


def test_horizontal_gate_forwards_undecided_prefix_then_latches():
    gate = HorizontalGestureGate(idle_ms=100, dominance=1.2, threshold=3)

    assert not gate.feed(1, 1, 0).consume
    first = gate.feed(2, -1, 10)
    assert first.consume
    assert first.delta_x == 3
    assert first.started
    again = gate.feed(4, 20, 20)
    assert again.consume
    assert again.delta_x == 4
    assert not again.started


def test_vertical_gate_never_steals_scroll_and_idle_starts_fresh():
    gate = HorizontalGestureGate(idle_ms=100, dominance=1.2, threshold=3)

    assert not gate.feed(0.5, 3, 0).consume
    assert not gate.feed(20, 1, 20).consume
    after_idle = gate.feed(-4, 0.5, 121)
    assert after_idle.consume
    assert after_idle.delta_x == -4


def test_non_finite_native_deltas_are_ignored():
    gate = HorizontalGestureGate(threshold=3)

    assert not gate.feed(float("nan"), float("inf"), 0).consume


def test_fractional_physical_prefix_latches_before_webkit_can_take_sequence():
    gate = HorizontalGestureGate()

    assert not gate.feed(0, 0, 0).consume  # NSEventPhaseMayBegin
    assert not gate.feed(0.02, 0.01, 2).consume
    first = gate.feed(0.04, -0.005, 4)
    assert first.consume
    assert first.started
    assert first.delta_x == 0.06


class _FakeAppKit:
    NSEventTypeSwipe = 31
    NSEventPhaseBegan = 1
    NSEventPhaseChanged = 4
    NSEventPhaseEnded = 8
    NSEventPhaseCancelled = 16
    NSEventPhaseMayBegin = 32
    NSEventModifierFlagOption = 1 << 19

    class NSEvent:
        @staticmethod
        def mouseLocation():
            return SimpleNamespace(x=0, y=0)


class _FakeEvent:
    def __init__(
        self,
        *,
        event_type=22,
        scrolling=(0, 0),
        legacy=(0, 0),
        precise=False,
        phase=0,
        momentum=0,
        window=None,
        location=(0, 0),
        modifiers=0,
    ):
        self._type = event_type
        self._scrolling = scrolling
        self._legacy = legacy
        self._precise = precise
        self._phase = phase
        self._momentum = momentum
        self._window = window
        self._location = location
        self._modifiers = modifiers

    def type(self):
        return self._type

    def scrollingDeltaX(self):
        return self._scrolling[0]

    def scrollingDeltaY(self):
        return self._scrolling[1]

    def deltaX(self):
        return self._legacy[0]

    def deltaY(self):
        return self._legacy[1]

    def hasPreciseScrollingDeltas(self):
        return self._precise

    def phase(self):
        return self._phase

    def momentumPhase(self):
        return self._momentum

    def window(self):
        return self._window

    def locationInWindow(self):
        return SimpleNamespace(x=self._location[0], y=self._location[1])

    def modifierFlags(self):
        return self._modifiers


def test_native_event_sample_uses_precise_scroll_and_keeps_phases():
    sample = _native_event_sample(
        _FakeEvent(
            scrolling=(0.125, -0.03125),
            legacy=(9, 8),
            precise=True,
            phase=1,
            momentum=4,
        ),
        _FakeAppKit,
    )

    assert sample.kind == "scroll"
    assert (sample.delta_x, sample.delta_y) == (0.125, -0.03125)
    assert sample.precise
    assert (sample.phase, sample.momentum_phase) == (1, 4)


def test_native_event_sample_falls_back_to_legacy_and_recognizes_swipe():
    fallback = _native_event_sample(_FakeEvent(legacy=(-2, 1)), _FakeAppKit)
    swipe = _native_event_sample(
        _FakeEvent(event_type=31, scrolling=(99, 99), legacy=(-1, 0)),
        _FakeAppKit,
    )

    assert (fallback.delta_x, fallback.delta_y) == (-2, 1)
    assert swipe.kind == "swipe"
    assert (swipe.delta_x, swipe.delta_y) == (-1, 0)


class _FakeWindow:
    def __init__(self, number, *, key=False, main=False):
        self.number = number
        self.key = key
        self.main = main

    def windowNumber(self):
        return self.number

    def isKeyWindow(self):
        return self.key

    def isMainWindow(self):
        return self.main

    def convertPointFromScreen_(self, point):
        return point


def test_native_window_matching_uses_window_number_and_safe_nil_fallback():
    native = _FakeWindow(17, key=True)
    proxy = _FakeWindow(17)
    other = _FakeWindow(23)

    assert _event_belongs_to_window(_FakeEvent(window=proxy), native) == (
        True,
        "number",
    )
    assert _event_belongs_to_window(_FakeEvent(window=other), native) == (
        False,
        "other",
    )
    assert _event_belongs_to_window(_FakeEvent(window=None), native) == (
        True,
        "nil-key",
    )


def _phase_masks():
    return NativeGesturePhaseMasks.from_appkit(_FakeAppKit)


def _scroll(dx, dy=0, *, phase=0, momentum=0):
    return NativeGestureSample("scroll", dx, dy, phase, momentum, True)


def test_sequence_keeps_prefix_and_one_start_from_may_begin_through_began():
    sequence = NativeGestureSequence(_phase_masks())

    first = sequence.feed(
        _scroll(0.02, 0.01, phase=_FakeAppKit.NSEventPhaseMayBegin), 0, "left"
    )
    began = sequence.feed(
        _scroll(0.04, -0.005, phase=_FakeAppKit.NSEventPhaseBegan), 2, "right"
    )

    assert first.hold_prefix
    assert began.target == "left"
    assert began.motion == HorizontalDecision(True, 0.06, True)


def test_sequence_preserves_axis_and_target_across_separate_momentum_transition():
    sequence = NativeGestureSequence(_phase_masks())
    events = [
        (_scroll(0.06, phase=_FakeAppKit.NSEventPhaseBegan), 0, "left"),
        (_scroll(0.02, phase=_FakeAppKit.NSEventPhaseChanged), 5, "right"),
        (_scroll(0.01, phase=_FakeAppKit.NSEventPhaseEnded), 10, "right"),
        (_scroll(0.5, 0.8, momentum=_FakeAppKit.NSEventPhaseBegan), 11, "right"),
        (_scroll(0.3, 1.0, momentum=_FakeAppKit.NSEventPhaseChanged), 16, "right"),
        (_scroll(0, momentum=_FakeAppKit.NSEventPhaseEnded), 25, "right"),
    ]

    routed = [sequence.feed(*event) for event in events]

    assert [item.target for item in routed] == ["left"] * len(events)
    assert [item.motion.started for item in routed].count(True) == 1
    assert all(item.motion.consume for item in routed)
    assert sequence.state == "idle"


def test_sequence_preserves_axis_on_combined_physical_end_and_momentum_begin():
    sequence = NativeGestureSequence(_phase_masks())
    started = sequence.feed(
        _scroll(0.06, phase=_FakeAppKit.NSEventPhaseBegan), 0, "plate"
    )
    transition = sequence.feed(
        _scroll(
            0.2,
            0.5,
            phase=_FakeAppKit.NSEventPhaseEnded,
            momentum=_FakeAppKit.NSEventPhaseBegan,
        ),
        5,
        "other",
    )

    assert started.motion.started
    assert transition.target == "plate"
    assert transition.motion.consume
    assert not transition.motion.started
    assert sequence.state == "momentum"


def test_sequence_without_momentum_resets_on_next_physical_start_or_idle():
    sequence = NativeGestureSequence(_phase_masks())
    sequence.feed(_scroll(0.06, phase=_FakeAppKit.NSEventPhaseBegan), 0, "first")
    sequence.feed(_scroll(0, phase=_FakeAppKit.NSEventPhaseEnded), 5, "first")

    next_gesture = sequence.feed(
        _scroll(-0.06, phase=_FakeAppKit.NSEventPhaseBegan), 8, "second"
    )
    assert next_gesture.target == "second"
    assert next_gesture.motion.started

    after_idle = sequence.feed(_scroll(0.06), 109, "third")
    assert after_idle.target == "third"
    assert after_idle.motion.started


def test_scope_cache_applies_exclusions_and_ignores_stale_snapshots():
    cache = NativeGestureScopeCache()
    assert cache.begin(SCOPE_TOKEN)
    first = cache.replace(
        {
            "token": SCOPE_TOKEN,
            "revision": 2,
            "scopes": [
                {
                    "target": "aaaabbbb",
                    "include": {"left": 0.1, "top": 0.2, "right": 0.9, "bottom": 0.9},
                    "exclude": [
                        {"left": 0.4, "top": 0.4, "right": 0.6, "bottom": 0.6}
                    ],
                },
                {
                    "target": "ccccdddd",
                    "include": {"left": 0.15, "top": 0.25, "right": 0.3, "bottom": 0.4},
                    "exclude": [],
                },
            ],
        }
    )

    assert first == {"ok": True, "stale": False, "revision": 2}
    assert cache.target_at(0.2, 0.3) == "aaaabbbb"
    assert cache.target_at(0.5, 0.5) is None
    assert cache.target_at(0.05, 0.3) is None
    assert cache.replace({"token": SCOPE_TOKEN, "revision": 1, "scopes": []})["stale"]
    assert cache.target_at(0.2, 0.3) == "aaaabbbb"
    assert cache.target_at(0.9, 0.3) is None  # half-open right edge
    assert cache.target_at(0.2, 0.9) is None  # half-open bottom edge


def test_scope_cache_invalid_snapshot_fails_open_without_clamping():
    cache = NativeGestureScopeCache()
    assert cache.begin(SCOPE_TOKEN)
    result = cache.replace(
        {
            "token": SCOPE_TOKEN,
            "revision": 1,
            "scopes": [
                {
                    "target": "aaaabbbb",
                    "include": {"left": -1, "top": 0, "right": 2, "bottom": 1},
                    "exclude": [],
                }
            ],
        }
    )

    assert not result["ok"]
    assert cache.target_at(0.5, 0.5) is None
    assert cache.snapshot()[1] == 1


@pytest.mark.parametrize(
    "scope",
    [
        {
            "target": "not-a-sid",
            "include": {"left": 0, "top": 0, "right": 1, "bottom": 1},
            "exclude": [],
        },
        {
            "target": "aaaabbbb",
            "include": {"left": "0", "top": 0, "right": 1, "bottom": 1},
            "exclude": [],
        },
        {
            "target": "aaaabbbb",
            "include": {"left": False, "top": 0, "right": 1, "bottom": 1},
            "exclude": [],
        },
        {
            "target": "aaaabbbb",
            "include": {"left": 0, "top": 0, "right": 1, "bottom": 1},
            "exclude": [{"left": 0.5, "top": 0.5, "right": 0.5, "bottom": 0.8}],
        },
    ],
)
def test_scope_cache_rejects_malformed_scope_fields(scope):
    cache = NativeGestureScopeCache()
    cache.begin(SCOPE_TOKEN)

    result = cache.replace({"token": SCOPE_TOKEN, "revision": 1, "scopes": [scope]})

    assert not result["ok"]
    assert cache.target_at(0.5, 0.5) is None


def test_scope_cache_equal_revision_cannot_overwrite_snapshot():
    cache = NativeGestureScopeCache()
    cache.begin(SCOPE_TOKEN)
    rect = {"left": 0, "top": 0, "right": 1, "bottom": 1}
    cache.replace(
        {
            "token": SCOPE_TOKEN,
            "revision": 4,
            "scopes": [{"target": "aaaabbbb", "include": rect, "exclude": []}],
        }
    )

    repeated = cache.replace(
        {
            "token": SCOPE_TOKEN,
            "revision": 4,
            "scopes": [{"target": "ccccdddd", "include": rect, "exclude": []}],
        }
    )

    assert repeated["stale"]
    assert cache.target_at(0.5, 0.5) == "aaaabbbb"


def test_scope_cache_concurrent_updates_leave_the_highest_revision_installed():
    cache = NativeGestureScopeCache()
    cache.begin(SCOPE_TOKEN)
    rect = {"left": 0, "top": 0, "right": 1, "bottom": 1}

    def publish(revision):
        return cache.replace(
            {
                "token": SCOPE_TOKEN,
                "revision": revision,
                "scopes": [{"target": "aaaabbbb", "include": rect, "exclude": []}],
            }
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(publish, reversed(range(1, 65))))

    assert cache.snapshot()[1] == 64
    assert cache.target_at(0.5, 0.5) == "aaaabbbb"


def test_scope_cache_rejects_boolean_revision():
    cache = NativeGestureScopeCache()
    cache.begin(SCOPE_TOKEN)

    result = cache.replace({"token": SCOPE_TOKEN, "revision": True, "scopes": []})

    assert result == {"ok": False, "error": "invalid revision"}


def test_scope_cache_navigation_token_rejects_late_old_page_update():
    cache = NativeGestureScopeCache()
    old_token = "0" * 32
    new_token = "1" * 32
    assert cache.begin(old_token)
    assert cache.begin(new_token)

    late = cache.replace({"token": old_token, "revision": 99, "scopes": []})
    assert late == {"ok": False, "error": "stale token"}
    assert cache.snapshot()[:2] == (new_token, -1)


class _FakeWebView:
    def __init__(self, width=1000, height=800, *, flipped=True, origin=(0, 0)):
        self.width = width
        self.height = height
        self.flipped = flipped
        self.origin = origin

    def convertPoint_fromView_(self, point, _view):
        return point

    def bounds(self):
        return SimpleNamespace(
            origin=SimpleNamespace(x=self.origin[0], y=self.origin[1]),
            size=SimpleNamespace(width=self.width, height=self.height),
        )

    def isFlipped(self):
        return self.flipped


def _scope_cache():
    cache = NativeGestureScopeCache()
    cache.begin(SCOPE_TOKEN)
    cache.replace(
        {
            "token": SCOPE_TOKEN,
            "revision": 1,
            "scopes": [
                {
                    "target": "11111111",
                    "include": {"left": 0.1, "top": 0.1, "right": 0.45, "bottom": 0.9},
                    "exclude": [
                        {"left": 0.2, "top": 0.2, "right": 0.3, "bottom": 0.3}
                    ],
                },
                {
                    "target": "22222222",
                    "include": {"left": 0.55, "top": 0.1, "right": 0.9, "bottom": 0.9},
                    "exclude": [],
                },
            ],
        }
    )
    return cache


def _monitor_harness():
    window = _FakeWindow(17, key=True)
    sent = []
    clock = iter(range(0, 1000, 5))
    monitor = _make_native_trackpad_monitor(
        appkit=_FakeAppKit,
        native_window=window,
        native_webview=_FakeWebView(),
        scope_cache=_scope_cache(),
        sequence=NativeGestureSequence(_phase_masks()),
        logger=lambda _message: None,
        clock_ms=lambda: next(clock),
        dispatch=lambda payload, started: sent.append((payload, started)),
    )
    return window, sent, monitor


def test_monitor_only_holds_prefix_inside_plate_scope_and_passes_exclusions():
    window, sent, monitor = _monitor_harness()
    outside = _FakeEvent(
        precise=True,
        scrolling=(0.02, 0.005),
        phase=_FakeAppKit.NSEventPhaseBegan,
        window=window,
        location=(50, 200),
    )
    entered_later = _FakeEvent(
        precise=True,
        scrolling=(0.08, 0),
        phase=_FakeAppKit.NSEventPhaseChanged,
        window=window,
        location=(150, 200),
    )
    assert monitor(outside) is outside
    assert monitor(entered_later) is entered_later

    # Start a fresh physical sequence over an excluded floating/control rect.
    ended = _FakeEvent(
        precise=True,
        phase=_FakeAppKit.NSEventPhaseEnded,
        window=window,
        location=(150, 200),
    )
    assert monitor(ended) is ended
    excluded = _FakeEvent(
        precise=True,
        scrolling=(0.08, 0),
        phase=_FakeAppKit.NSEventPhaseBegan,
        window=window,
        location=(250, 200),
    )
    assert monitor(excluded) is excluded
    assert sent == []


def test_monitor_dispatches_inside_scope_and_keeps_captured_pane_through_momentum():
    window, sent, monitor = _monitor_harness()
    prefix = _FakeEvent(
        precise=True,
        scrolling=(0.02, 0.01),
        phase=_FakeAppKit.NSEventPhaseMayBegin,
        window=window,
        location=(150, 400),
    )
    began = _FakeEvent(
        precise=True,
        scrolling=(0.04, -0.005),
        phase=_FakeAppKit.NSEventPhaseBegan,
        window=window,
        location=(150, 400),
        modifiers=_FakeAppKit.NSEventModifierFlagOption,
    )
    physical_end = _FakeEvent(
        precise=True,
        scrolling=(0.01, 0),
        phase=_FakeAppKit.NSEventPhaseEnded,
        window=window,
        location=(650, 400),
    )
    momentum_begin = _FakeEvent(
        precise=True,
        scrolling=(0.3, 0.8),
        momentum=_FakeAppKit.NSEventPhaseBegan,
        window=window,
        location=(650, 400),
    )

    assert monitor(prefix) is None
    assert monitor(began) is None
    assert monitor(physical_end) is None
    assert monitor(momentum_begin) is None
    assert [payload["target"] for payload, _started in sent] == ["11111111"] * 3
    assert [started for _payload, started in sent] == [True, False, False]
    assert sent[0][0]["deltaX"] == 0.06
    assert sent[0][0]["altKey"]
    assert sent[0][0]["clientXRatio"] == 0.15


def test_monitor_returns_vertical_stage_sequence_to_webkit_without_dispatch():
    window, sent, monitor = _monitor_harness()
    vertical = _FakeEvent(
        precise=True,
        scrolling=(0.01, 0.06),
        phase=_FakeAppKit.NSEventPhaseBegan,
        window=window,
        location=(150, 400),
    )
    changed = _FakeEvent(
        precise=True,
        scrolling=(1, 0),
        phase=_FakeAppKit.NSEventPhaseChanged,
        window=window,
        location=(150, 400),
    )

    assert monitor(vertical) is vertical
    assert monitor(changed) is changed
    assert sent == []


def test_monitor_normalizes_points_relative_to_nonzero_native_bounds_origin():
    window = _FakeWindow(17, key=True)
    sent = []
    monitor = _make_native_trackpad_monitor(
        appkit=_FakeAppKit,
        native_window=window,
        native_webview=_FakeWebView(origin=(100, 50)),
        scope_cache=_scope_cache(),
        sequence=NativeGestureSequence(_phase_masks()),
        logger=lambda _message: None,
        clock_ms=lambda: 0,
        dispatch=lambda payload, started: sent.append((payload, started)),
    )
    event = _FakeEvent(
        precise=True,
        scrolling=(0.06, 0),
        phase=_FakeAppKit.NSEventPhaseBegan,
        window=window,
        location=(250, 450),
    )

    assert monitor(event) is None
    assert sent[0][0]["clientXRatio"] == 0.15
    assert sent[0][0]["clientYRatio"] == 0.5


def test_monitor_stops_consuming_when_captured_pane_is_removed_from_scope():
    window = _FakeWindow(17, key=True)
    sent = []
    cache = _scope_cache()
    monitor = _make_native_trackpad_monitor(
        appkit=_FakeAppKit,
        native_window=window,
        native_webview=_FakeWebView(),
        scope_cache=cache,
        sequence=NativeGestureSequence(_phase_masks()),
        logger=lambda _message: None,
        clock_ms=iter((0, 5)).__next__,
        dispatch=lambda payload, started: sent.append((payload, started)),
    )
    began = _FakeEvent(
        precise=True,
        scrolling=(0.06, 0),
        phase=_FakeAppKit.NSEventPhaseBegan,
        window=window,
        location=(150, 400),
    )
    changed = _FakeEvent(
        precise=True,
        scrolling=(0.2, 0),
        phase=_FakeAppKit.NSEventPhaseChanged,
        window=window,
        location=(150, 400),
    )

    assert monitor(began) is None
    assert cache.replace({"token": SCOPE_TOKEN, "revision": 2, "scopes": []})["ok"]
    assert monitor(changed) is changed
    assert len(sent) == 1


def test_native_bridge_is_wired_through_shell_and_plate_page():
    app = (ROOT / "nd2wsi" / "app.py").read_text()
    shell = (ROOT / "nd2wsi" / "static" / "shell-v1.js").read_text()
    shell_html = (ROOT / "nd2wsi" / "static" / "shell.html").read_text()
    page = (ROOT / "nd2wsi" / "static" / "app.js").read_text()

    assert "wire_native_trackpad_bridge(" in app
    assert "scope_cache=api._native_gesture_scopes" in app
    assert "set_native_gesture_scopes" in app
    assert "begin_native_gesture_scope_session" in app
    assert "window.events.before_load" in app
    assert "window.nd2wsiNativeTrackpad" in shell
    assert "NativeScope.buildScopes" in shell
    assert 'kind === "native-gesture-scope"' in shell
    assert "nativeGestureScopeToken" in shell
    assert '"compare-controls", "compare-divider", "compare-picker", "dropzone"' in shell
    assert shell_html.index("native-scope-v1.js") < shell_html.index("shell-v1.js")
    assert '"target": routed.target' in (
        ROOT / "nd2wsi" / "native_gestures.py"
    ).read_text()
    assert 'nd2wsi: "native-trackpad"' in shell
    assert "gestureStart: !!input?.gestureStart" in shell
    assert 'data.nd2wsi !== "native-trackpad"' in page
    assert "if (data.gestureStart)" in page
    assert "nativeGridGesture" in page
    assert "const NATIVE_GESTURE_EXCLUSIONS" in page
    assert 'nd2wsi: "native-gesture-scope"' in page
    assert "Number(state.info?.plate?.T) > 1" in page
    assert "window.addEventListener(\"pagehide\"" in page
    assert "target.closest(NATIVE_GESTURE_EXCLUSIONS)" in page
    assert "NSEventMaskScrollWheel | AppKit.NSEventMaskSwipe" in (
        ROOT / "nd2wsi" / "native_gestures.py"
    ).read_text()
    assert "setAllowsBackForwardNavigationGestures_(False)" in (
        ROOT / "nd2wsi" / "native_gestures.py"
    ).read_text()
