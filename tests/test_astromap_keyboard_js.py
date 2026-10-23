"""Astromapper arrow review owns one key before the real OSD/plate handlers."""

import pytest
from test_keyboard_integration_js import NODE, pane

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

ITEMS = [
    {"id": "z", "type": "box", "astromap": True, "x": 10, "y": 20, "w": 10, "h": 20},
    {"id": "ordinary", "type": "box", "x": 0, "y": 0, "w": 1, "h": 1},
    {"id": "a", "type": "box", "astromap": True, "astromapEnabled": False,
     "astromapImportIndex": 118, "x": 100, "y": 200, "w": 40, "h": 60},
    {"id": "m", "type": "box", "astromap": True, "x": 300, "y": 400, "w": 80, "h": 100},
]


def arrow(key="ArrowRight", **kwargs):
    return {"key": key, "code": key, "keyCode": 37 if key == "ArrowLeft" else 39, **kwargs}


def review(events, **config):
    astromap = {"items": ITEMS, "activeId": "z", **config.pop("astromap", {})}
    return pane(events, astromap=astromap, **config)


@pytest.mark.parametrize("target", [{"tag": "BUTTON", "astromapRow": True}, {},
                                   {"tag": "INPUT", "type": "checkbox", "astromapRow": True}])
@pytest.mark.parametrize("routing", [{}, {"iframe": True, "plate": True},
                                    {"iframe": True, "compare": {"enabled": True, "frameLinked": True}}])
def test_arrow_follows_visible_row_order_and_centers_once_before_other_handlers(target, routing):
    out = review([arrow(target=target)], **routing)
    assert out["activeRoi"] == "a"  # Export-excluded rows remain part of review.
    assert out["roiPans"] == [[{"x": 120, "y": 230}, True]]
    assert out["roiZooms"] == [[3.25, None, True]]
    assert out["roiScrolls"] == ["a"]
    assert out["roiFocus"] == (["a"] if target.get("astromapRow") else [])
    assert out["pans"] == out["messages"] == []
    assert out["targetDeliveries"] == out["osdKeydowns"] == 0
    assert out["events"] == [{"prevented": True, "stopped": True}]
    assert out["annotations"] == ITEMS


def test_repeated_arrows_advance_then_clamp_both_ends_without_extra_pan():
    out = review([arrow("ArrowLeft"), arrow(), arrow(repeat=True), arrow(repeat=True),
                  arrow("ArrowLeft"), arrow("ArrowLeft"), arrow("ArrowLeft", repeat=True)])
    assert out["activeRoi"] == "z"
    assert len(out["roiPans"]) == 4
    assert out["roiScrolls"] == ["a", "m", "a", "z"]
    assert out["pans"] == [] and out["osdKeydowns"] == 0
    assert all(event == {"prevented": True, "stopped": True} for event in out["events"])
    assert out["annotations"] == ITEMS


@pytest.mark.parametrize("target", [{"tag": "INPUT", "type": "text"}, {"tag": "TEXTAREA"},
                                   {"tag": "SELECT"}, {"editable": True},
                                   {"insideEditable": True}, {"insideTextbox": True}])
def test_note_and_editable_targets_retain_arrow_ownership(target):
    out = review([arrow(target=target, outsideCanvas=True)])
    assert out["activeRoi"] == "z" and out["roiPans"] == []
    assert out["events"] == [{"prevented": False, "stopped": False}]


@pytest.mark.parametrize("guard", [{"metaKey": True}, {"ctrlKey": True}, {"altKey": True},
                                  {"shiftKey": True}, {"isComposing": True}, {"keyCode": 229},
                                  {"defaultPrevented": True}])
def test_modified_composing_or_prevented_arrows_do_not_change_roi(guard):
    out = review([arrow(outsideCanvas=True, **guard)])
    assert out["activeRoi"] == "z" and out["roiPans"] == out["roiFocus"] == []


@pytest.mark.parametrize("guard", [{"astromap": {"hidden": True}}, {"astromap": {"activeId": None}},
                                  {"astromap": {"activeId": "missing"}},
                                  {"astromap": {"ownerReady": False}}, {"ready": False},
                                  {"grid": True}, {"quitPreparing": True}, {"landmark": True}])
def test_only_a_visible_ready_astromapper_with_a_current_selection_owns_arrows(guard):
    out = review([arrow(outsideCanvas=True)], **guard)
    assert out["roiPans"] == out["roiScrolls"] == []
