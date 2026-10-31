"""Calibration persistence and reported geometry never touch source pixels."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nd2wsi import laser_target as lt
from nd2wsi.translucent_window import TranslucentApi


@pytest.fixture(autouse=True)
def isolated_target_store(monkeypatch, tmp_path):
    monkeypatch.setenv("ND2WSI_FOLLOW_FLIPS_ROOT", str(tmp_path / "settings"))


def target():
    return {"offset_css": [12, -8], "side_um": 18, "rotation_deg": 105,
            "window": [20, 30, 640, 480], "dpr": 2}


def test_store_round_trip_defaults_and_agent_isolation(tmp_path):
    store = lt.LaserTargetStore()
    assert store.load() == (lt.DEFAULT, None)
    store.save(target())
    assert store.load() == ({**target(), "rotation_deg": 15}, None)
    agent = lt.LaserTargetStore(SimpleNamespace(role="agent", root=tmp_path / "agent"))
    assert agent.load() == (lt.DEFAULT, None)
    agent.save({**target(), "side_um": 22})
    assert store.load()[0]["side_um"] == 18
    assert agent.load()[0]["side_um"] == 22


@pytest.mark.parametrize("change", [{"side_um": 1}, {"side_um": 201}, {"side_um": True},
    {"offset_css": [float("nan"), 0]}, {"offset_css": [0]}, {"window": [0, 0, -1, 480]},
    {"dpr": 0}, {"dpr": None}, {"extra": 1}])
def test_store_rejects_invalid_without_replacing_file(change):
    store = lt.LaserTargetStore()
    store.save(target())
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        store.save({**target(), **change})
    assert store.path.read_bytes() == before


def test_broken_large_and_symlinked_store_is_not_loaded(tmp_path):
    store = lt.LaserTargetStore()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    for data in (b"broken", b"x" * (lt.MAX_BYTES + 1), b'{"format":"other"}'):
        store.path.write_bytes(data)
        assert store.load()[0] == lt.DEFAULT and store.load()[1]
        assert store.path.read_bytes() == data
    store.path.unlink()
    destination = tmp_path / "unrelated.json"
    destination.write_text("unchanged")
    store.path.symlink_to(destination)
    assert store.load()[1]
    with pytest.raises(OSError):
        store.save(target())
    assert destination.read_text() == "unchanged"


def test_bridge_saves_only_unlocked_and_report_round_trips(monkeypatch):
    events = []
    monkeypatch.setattr("nd2wsi.translucent_window.session_log.event", lambda kind, **data: events.append((kind, data)))
    store = lt.LaserTargetStore()
    overlay = TranslucentApi({}, logger=Mock(), on_closed=Mock(), target_store=store)
    payload = {"target": target(), "calibrate": True, "zoom": 2}
    assert overlay.save_laser_target(payload)["saved"]
    assert overlay.translucent_context()["laser_target"] == store.load()[0]
    assert events[-1][0] == "laser_target_set" and events[-1][1]["zoom"] == 2
    overlay._locked = True
    assert not overlay.save_laser_target(payload)["ok"]
    overlay._locked = False
    view = {"version": 1, "center_px": [100.04, 200], "zoom": 2,
            "target_px": [100.04, 200], "target_corners_px": [[90,190],[110,190],[110,210],[90,210]],
            "target_offset_css": [0,0], "target_side_um": 18, "target_rotation_deg": 45, "target_calibrated": True}
    assert overlay.report_overlay_view(view)["ok"]
    mark = overlay._mark_context()
    assert mark["target_px"] == mark["image_px"] == [100, 200]
    assert mark["target_corners_px"] == view["target_corners_px"]
    for change in ({"target_px": [float("inf"), 0]}, {"target_calibrated": "yes"},
                   {"target_corners_px": [[0,0]]}, {"target_px": None}):
        assert not overlay.report_overlay_view({**view, **change})["ok"]
