"""Native macOS gesture routing and its JavaScript hand-off."""

from pathlib import Path

from nd2wsi.native_gestures import HorizontalGestureGate


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


def test_native_bridge_is_wired_through_shell_and_plate_page():
    app = (ROOT / "nd2wsi" / "app.py").read_text()
    shell = (ROOT / "nd2wsi" / "static" / "shell-v1.js").read_text()
    page = (ROOT / "nd2wsi" / "static" / "app.js").read_text()

    assert "wire_native_trackpad_bridge(window, _dlog)" in app
    assert "window.nd2wsiNativeTrackpad" in shell
    assert 'nd2wsi: "native-trackpad"' in shell
    assert 'data.nd2wsi !== "native-trackpad"' in page
    assert "nativeGridGesture" in page
