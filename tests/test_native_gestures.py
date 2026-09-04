"""Native macOS gesture routing and its JavaScript hand-off."""

from pathlib import Path

from nd2wsi.native_gestures import (
    HorizontalGestureGate,
    _event_belongs_to_window,
    _native_event_sample,
)


ROOT = Path(__file__).resolve().parents[1]


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
    ):
        self._type = event_type
        self._scrolling = scrolling
        self._legacy = legacy
        self._precise = precise
        self._phase = phase
        self._momentum = momentum
        self._window = window

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


def test_native_bridge_is_wired_through_shell_and_plate_page():
    app = (ROOT / "nd2wsi" / "app.py").read_text()
    shell = (ROOT / "nd2wsi" / "static" / "shell-v1.js").read_text()
    page = (ROOT / "nd2wsi" / "static" / "app.js").read_text()

    assert "wire_native_trackpad_bridge(window, _dlog)" in app
    assert "window.nd2wsiNativeTrackpad" in shell
    assert 'nd2wsi: "native-trackpad"' in shell
    assert "gestureStart: !!input?.gestureStart" in shell
    assert 'data.nd2wsi !== "native-trackpad"' in page
    assert "if (data.gestureStart)" in page
    assert "nativeGridGesture" in page
    assert "NSEventMaskScrollWheel | AppKit.NSEventMaskSwipe" in (
        ROOT / "nd2wsi" / "native_gestures.py"
    ).read_text()
    assert "setAllowsBackForwardNavigationGestures_(False)" in (
        ROOT / "nd2wsi" / "native_gestures.py"
    ).read_text()
