"""Regression suite for SLACS-module repairs SW-1 through SW-5."""

from __future__ import annotations

import ctypes
import importlib.util
import json
import re
import subprocess
import sys
import threading
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import test_translucent_js
from node_host import run_node
from test_astromap_ui_js import _run_export

from nd2wsi import slacs_discovery, slacs_laser, stage_keys
from nd2wsi.annotation_workspace import AnnotationWorkspace
from nd2wsi.translucent_window import TranslucentApi

REPO = Path(__file__).resolve().parents[1]
STATIC = REPO / "nd2wsi" / "static"


def _laser_context(**overrides):
    base = {
        "version": 1,
        "slide_url": "http://127.0.0.1:8123/secret/s/slide1/",
        "query": "?g=pixels&t=0&p=0&z=0",
        "annotation_scope": "plane",
        "center": [200.0, 200.0],
        "zoom": 1.0,
        "rotation": 0,
        "flip": False,
        "opacity": 0.55,
        "opacity_mode": "uniform",
        "pixel_size_um": [0.5, 0.5],
        "dimensions": [1000, 1000],
        "image_name": "cell.nd2",
        "source_format": "nd2",
    }
    base.update(overrides)
    return base


def _report_ready_view(api: TranslucentApi, session_id: str, *, center=(200.0, 200.0), seq=0):
    cx, cy = center
    return api.report_overlay_view({
        "version": 1,
        "center_px": [cx, cy],
        "zoom": 1.0,
        "locked": True,
        "follow_session": session_id,
        "follow_seq": seq,
        "target_px": [cx, cy],
        "target_offset_css": [0, 0],
        "target_corners_px": [[cx - 5, cy - 5], [cx + 5, cy - 5], [cx + 5, cy + 5], [cx - 5, cy + 5]],
        "target_side_um": 10.0,
        "target_rotation_deg": 0.0,
        "target_calibrated": True,
    })


# ---- SW-1: translucent_window.py laser epoch vs context_epoch ----------------


def test_sw1_first_shot_unlock_realign_relock_during_prepare_blocks_delivery():
    """On shot #1 (_pending_laser_epoch initially None), unlock->realign->relock mid-prepare must not deliver."""
    delivered = []
    api = TranslucentApi(_laser_context(), logger=Mock(), on_closed=Mock(), on_laser_box=lambda p: (delivered.append(p), True)[1])
    api._configured = True
    api._window = Mock(native=None)
    api._dispatch = Mock(return_value=True)
    api._slacs_snapshot = {"available": True, "session_id": "slacs-1", "status": "running"}
    with patch("nd2wsi.translucent_window.session_log.event"):
        started = api.start_stage_follow({"source": "adapter", "step_um": 5.0, "step_source": "manual"})
    assert started["ok"] is True
    assert api.set_translucent_locked(True)["ok"] is True
    assert _report_ready_view(api, started["session_id"], center=(200.0, 200.0))["ok"] is True
    assert api._pending_laser_epoch is None
    assert api._pending_laser_context_epoch is None

    reached = threading.Event()
    resume = threading.Event()

    class BarrierView(dict):
        def get(self, key, default=None):
            if key == "target_corners_px":
                reached.set()
                assert resume.wait(5)
            return super().get(key, default)

    api._overlay_view = BarrierView(api._overlay_view)
    record = {
        "locked": True,
        "receiver_id": started["session_id"],
        "press": 1,
        "follow_seq": 0,
        "observed_at": api._overlay_view["at"] + 1.0,
        "at": "2026-11-14T05:00:00.000+00:00",
        "slacs_session": "slacs-1",
    }
    out = {}

    def run_prepare():
        out["payload"], out["reason"] = api._prepare_laser_box(record)

    worker = threading.Thread(target=run_prepare)
    worker.start()
    assert reached.wait(5)
    try:
        # User unlocks, realigns to (350, 350), and relocks while shot #1 is mid-prepare.
        assert api.set_translucent_locked(False)["ok"] is True
        assert api.set_translucent_locked(True)["ok"] is True
        assert _report_ready_view(api, started["session_id"], center=(350.0, 350.0))["ok"] is True
    finally:
        resume.set()
        worker.join(5)

    try:
        assert out["reason"] is None
        assert out["payload"]["target_px"] == [200.0, 200.0]
        assert api._pending_laser_epoch is not None
        assert api._pending_laser_epoch != api._epoch
        assert api._deliver_laser_box(out["payload"]) is False
        assert delivered == []
    finally:
        api._did_close()


def test_sw1_unlock_during_in_flight_confirmation_still_renders_persisted_roi():
    """Unlocking the overlay after delivery must not drop the confirmed SLACS_ROI from #laser-annotations."""
    delivered = []
    api = TranslucentApi(_laser_context(), logger=Mock(), on_closed=Mock(), on_laser_box=lambda p: (delivered.append(p), True)[1])
    api._configured = True
    api._window = Mock(native=None)
    api._dispatch = Mock(return_value=True)
    api._window.evaluate_js.return_value = True
    api._slacs_snapshot = {"available": True, "session_id": "slacs-1", "status": "running"}
    with patch("nd2wsi.translucent_window.session_log.event"):
        started = api.start_stage_follow({"source": "adapter", "step_um": 5.0, "step_source": "manual"})
    assert started["ok"] is True
    assert api.set_translucent_locked(True)["ok"] is True
    assert _report_ready_view(api, started["session_id"])["ok"] is True

    record = {
        "locked": True,
        "receiver_id": started["session_id"],
        "press": 1,
        "follow_seq": 0,
        "observed_at": api._overlay_view["at"] + 1.0,
        "at": "2026-11-14T05:00:00.000+00:00",
        "slacs_session": "slacs-1",
    }
    try:
        payload, reason = api._prepare_laser_box(record)
        assert reason is None and payload is not None
        assert api._deliver_laser_box(payload) is True
        assert len(delivered) == 1
        # User unlocks the overlay while report_slacs_laser_box confirmation is in flight.
        assert api.set_translucent_locked(False)["ok"] is True
        confirmed_item = {
            "id": "slacs-laser-r1-1",
            "type": "box",
            "x": 195,
            "y": 195,
            "w": 10,
            "h": 10,
            "text": "SLACS_ROI #1",
            "slacs": {"kind": "laser_shot", "isolation": 1, "shots": 1},
        }
        assert api._confirm_laser_box({"item": confirmed_item}) is True
        assert api._laser_items["slacs-laser-r1-1"]["text"] == "SLACS_ROI #1"
        api._window.evaluate_js.assert_called_once()
        assert "nd2TranslucentLaserItem" in api._window.evaluate_js.call_args.args[0]
    finally:
        api._did_close()


def test_sw1_refresh_context_during_in_flight_confirmation_drops_stale_roi():
    """Changing slide/plane via refresh_context bumps _context_epoch and drops late confirmation."""
    api = TranslucentApi(_laser_context(), logger=Mock(), on_closed=Mock(), on_laser_box=lambda _p: True)
    api._configured = True
    api._page_url = "http://127.0.0.1:8123/secret/translucent.html"
    api._window = Mock(native=None)
    api._dispatch = Mock(return_value=True)
    api._slacs_snapshot = {"available": True, "session_id": "slacs-1", "status": "running"}
    with patch("nd2wsi.translucent_window.session_log.event"):
        started = api.start_stage_follow({"source": "adapter", "step_um": 5.0, "step_source": "manual"})
        assert started["ok"] is True
        assert api.set_translucent_locked(True)["ok"] is True
        assert _report_ready_view(api, started["session_id"])["ok"] is True
        record = {
            "locked": True,
            "receiver_id": started["session_id"],
            "press": 1,
            "follow_seq": 0,
            "observed_at": api._overlay_view["at"] + 1.0,
            "at": "2026-11-14T05:00:00.000+00:00",
            "slacs_session": "slacs-1",
        }
        payload, reason = api._prepare_laser_box(record)
        assert reason is None
        assert api._deliver_laser_box(payload) is True
        # Refresh context (even with identical query/scope, e.g. reload) advances _context_epoch.
        api.refresh_context(_laser_context(laser_items=[]))
        api._window.evaluate_js.reset_mock()
        stale_item = {
            "id": "slacs-laser-r1-1",
            "type": "box",
            "x": 195,
            "y": 195,
            "w": 10,
            "h": 10,
            "text": "SLACS_ROI #1",
            "slacs": {"kind": "laser_shot", "isolation": 1, "shots": 1},
        }
        assert api._confirm_laser_box({"item": stale_item}) is False
        assert api._laser_items == {}
        api._window.evaluate_js.assert_not_called()
    api._did_close()


# ---- SW-2: slacs_discovery.py + translucent_window.py bound identifier -------


def test_sw2_process_creation_filetime_uses_lightweight_win32_handle_check(monkeypatch):
    """_process_creation_filetime uses OpenProcess + GetProcessTimes and never invokes PowerShell."""
    monkeypatch.setattr(
        slacs_discovery,
        "_query_windows_processes",
        lambda **_kw: pytest.fail("must never invoke PowerShell"),
    )
    assert slacs_discovery._process_creation_filetime(5150) is None or slacs_discovery.sys.platform == "win32"

    monkeypatch.setattr(slacs_discovery.sys, "platform", "win32")
    closed_handles = []
    open_calls = []

    class FakeKernel32:
        def OpenProcess(self, access, inherit, pid):
            open_calls.append((access, inherit, pid))
            return 9001 if pid == 5150 else 0

        def GetProcessTimes(self, handle, creation_ptr, _exit_ptr, _kernel_ptr, _user_ptr):
            assert handle == 9001
            ft = ctypes.cast(creation_ptr, ctypes.POINTER(wintypes.FILETIME)).contents
            ft.dwHighDateTime = 0x01DB36A0
            ft.dwLowDateTime = 0x89ABCDEF
            return 1

        def CloseHandle(self, handle):
            closed_handles.append(handle)
            return 1

    monkeypatch.setattr(slacs_discovery, "_kernel32", lambda: (ctypes, wintypes, FakeKernel32()))
    expected_ticks = (0x01DB36A0 << 32) | 0x89ABCDEF
    assert slacs_discovery._process_creation_filetime(5150) == expected_ticks
    assert open_calls == [(slacs_discovery._PROCESS_QUERY_LIMITED_INFORMATION, False, 5150)]
    assert closed_handles == [9001]
    assert slacs_discovery._process_creation_filetime(9999) is None
    assert slacs_discovery._process_creation_filetime(0) is None
    assert slacs_discovery._process_creation_filetime(True) is None


def test_sw2_start_stage_follow_binds_follower_identifier_to_discovered_pid_and_creation_time(monkeypatch):
    """SlacsKeyFollower only accepts keys from the exact bound (pid, creation_filetime), rejecting other PIDs or restarts."""
    script = r"C:\Python27\Scripts\SniperCellSorterGUIv3.1.py"
    birth_iso = "2026-10-27T02:00:00.1234567Z"
    bound_ft = 134_060_904_001_234_567

    monkeypatch.setattr(slacs_discovery, "_process_creation_filetime", lambda pid: bound_ft if pid == 5150 else None)
    monitor = slacs_discovery.SlacsDiscovery(
        process_query=lambda: [{
            "pid": 5150,
            "created": birth_iso,
            "name": "python.exe",
            "command_line": f'"C:\\Python27\\python.exe" "{script}"',
        }],
        liveness=lambda _pid, _created: True,
        autostart=False,
    )
    snap = monitor.poll_once()
    assert snap["available"] is True
    assert monitor.slacs_pid() == 5150
    assert monitor.slacs_creation_filetime() == bound_ft

    events = []

    class ManualObserver:
        def start(self, on_event):
            self.on_event = on_event
            return True

        def stop(self):
            pass

        def tap_d(self):
            self.on_event(0x44, True, ())
            self.on_event(0x44, False, ())

    observer = ManualObserver()
    foreground = [{"pid": 5150, "created": bound_ft}]
    real_follower = stage_keys.SlacsKeyFollower

    def make_follower(receiver, session_id, **options):
        events.append(options)
        return real_follower(
            receiver,
            session_id,
            observer=observer,
            foreground_provider=lambda: foreground[0],
            heartbeat_interval=None,
            **options,
        )

    monkeypatch.setattr(stage_keys, "SlacsKeyFollower", make_follower)
    api = TranslucentApi(_laser_context(), logger=Mock(), on_closed=Mock())
    api._discovery = monitor
    api._slacs_snapshot = snap

    try:
        with patch("nd2wsi.translucent_window.session_log.event"), patch("nd2wsi.stage_keys.session_log.event"):
            started = api.start_stage_follow({"source": "keys", "step_um": 10.0, "step_source": "manual"})
            assert started["ok"] is True
            assert len(events) == 1 and callable(events[0]["identifier"])
            receiver = api._stage_follow

            # 1. Bound PID + bound creation FILETIME -> accepted.
            observer.tap_d()
            assert receiver.snapshot()["seq"] == 1

            # 2. Different PID (e.g., another SLACS window or unrelated app) -> rejected.
            foreground[0] = {"pid": 6260, "created": bound_ft}
            observer.tap_d()
            assert receiver.snapshot()["seq"] == 1

            # 3. Same PID reused by a restarted process (different creation FILETIME) -> rejected.
            foreground[0] = {"pid": 5150, "created": bound_ft + 10}
            observer.tap_d()
            assert receiver.snapshot()["seq"] == 1

            # 4. Same PID when foreground provider omits creation timestamp (created=None) -> accepted.
            foreground[0] = {"pid": 5150, "created": None}
            observer.tap_d()
            assert receiver.snapshot()["seq"] == 2
    finally:
        api.close_translucent()
        monitor.stop()


# ---- SW-3: slacs-laser-v1.js + app.js + annotation_workspace.py --------------


def test_sw3_normalize_state_sanitizes_inputs_and_satisfies_server_schema():
    script = r"""
    const laser = require(process.argv[1] + '/slacs-laser-v1.js');
    const manyPresses = {};
    for (let i = 0; i < 300; i++) manyPresses['rec_' + i] = i + 1;
    const rawProto = JSON.parse('{"isolation_highwater": 4, "presses": {"__proto__": 9, "constructor": 4, "bad space": 2, "zero": 0, "neg": -5, "floatVal": 7.8, "huge": 1e30, "atMax": 9007199254740991, "belowMax": 9007199254740990, "ok_1": 7}}');
    const cases = {
      absent: laser.normalizeState(undefined),
      nullVal: laser.normalizeState(null),
      arrayVal: laser.normalizeState([1, 2]),
      emptyObj: laser.normalizeState({}),
      zeroState: laser.normalizeState({ isolation_highwater: 0, presses: {} }),
      negState: laser.normalizeState({ isolation_highwater: -12, presses: { r1: -3, r2: 0 } }),
      floatHighwater: laser.normalizeState({ isolation_highwater: 3.9 }),
      atMaxHighwater: laser.normalizeState({ isolation_highwater: Number.MAX_SAFE_INTEGER }),
      hugeHighwater: laser.normalizeState({ isolation_highwater: 1e30 }),
      belowMaxHighwater: laser.normalizeState({ isolation_highwater: Number.MAX_SAFE_INTEGER - 1 }),
      protoAndFilter: laser.normalizeState(rawProto),
      boundedPresses: laser.normalizeState({ isolation_highwater: 1, presses: manyPresses }),
      fromLaserItems: laser.normalizeState({ isolation_highwater: 2 }, [
        { type: 'box', text: 'manual' },
        { type: 'box', text: 'SLACS_ROI #8', slacs: { kind: 'laser_shot', isolation: 8 } },
      ]),
    };
    process.stdout.write(JSON.stringify(cases));
    """
    proc = run_node(script, str(STATIC))
    assert proc.returncode == 0, proc.stderr
    cases = json.loads(proc.stdout)

    for empty_key in (
        "absent",
        "nullVal",
        "arrayVal",
        "emptyObj",
        "zeroState",
        "negState",
        "floatHighwater",
        "atMaxHighwater",
        "hugeHighwater",
    ):
        assert cases[empty_key] is None

    assert cases["belowMaxHighwater"] == {"isolation_highwater": 9007199254740990, "presses": {}}
    assert cases["protoAndFilter"] == {
        "isolation_highwater": 4,
        "presses": {"belowMax": 9007199254740990, "ok_1": 7},
    }
    assert len(cases["boundedPresses"]["presses"]) == 256
    assert "rec_0" not in cases["boundedPresses"]["presses"]
    assert cases["boundedPresses"]["presses"]["rec_299"] == 300
    assert cases["fromLaserItems"] == {"isolation_highwater": 8, "presses": {}}

    # Every non-null normalized state must pass Python server POST /api/annotations validation.
    for key in ("belowMaxHighwater", "protoAndFilter", "boundedPresses", "fromLaserItems"):
        validated = slacs_laser.metadata(cases[key])
        assert validated == {"slacs": cases[key]}


def test_sw3_open_annotation_file_restores_and_merges_laser_highwater():
    result = _run_export(r"""
    const f = fixture(), c = f.c;
    const staticDir = path.dirname(process.argv[1]);
    c.window.Nd2SlacsLaser = require(path.join(staticDir, 'slacs-laser-v1.js'));
    c.window.Nd2AnnotationGeoJSON = require(path.join(staticDir, 'annotation-geojson-v1.js'));
    c.window.Nd2AnnotationStore = require(path.join(staticDir, 'annotation-store-v1.js'));
    c.Blob = Blob;
    c.annCounter = 1;
    c.state.plate = false;
    c.state.info = { name: 'sample.nd2', width: 2000, height: 2000, selection: { t: 0, p: 0, z: 0 } };
    c.state.annContext = 'ctx-1';
    c.state.annSite = null;
    c.state.annRevision = 'rev-1';
    c.state.annForeignItems = [];
    c.state.annotations = [];
    c.state.annLaserState = null;
    c.pixelSize = () => [0.5, 0.5];
    let saves = 0;
    c.annotationsChanged = () => { saves += 1; };
    c.setAnnStatus = () => {};
    c.$('ann-file').value = 'picked';

    vm.runInContext([
      production('currentSelection'),
      production('normalizedSelection'),
      production('selectionMatchesCurrent'),
      production('normalizedLaserState'),
      production('mergeLaserStates'),
      production('annotationItemsForThisSlide'),
      production('annotationDocument'),
      production('openAnnotationFile', true),
    ].join('\n'), c);

    function fakeFile(name, obj) {
      const text = JSON.stringify(obj);
      return { name, size: Buffer.byteLength(text, 'utf8'), text: async () => text };
    }

    // 1. Replace-import a SLACS export where tail ROIs #4 and #5 were deleted before export:
    //    items only has #1 and #2, while slacs.isolation_highwater is 5.
    const slacsExport = {
      format: 'nd2wsi-annotations/2',
      coordinate_space: 'level-0-pixels',
      source: { name: 'sample.nd2', width: 2000, height: 2000 },
      selection: { t: 0, p: 0, z: 0 },
      slacs: { isolation_highwater: 5, presses: { recA: 11 } },
      items: [
        { id: 'slacs-1', type: 'box', x: 10, y: 10, w: 20, h: 20, color: '#a0a6ae', text: 'SLACS_ROI #1',
          slacs: { kind: 'laser_shot', version: 1, isolation: 1, press: 1, target_px: [20, 20], side_um: 10,
                   rotation_deg: 0, calibrated: true, at: '2026-01-01T00:00:00Z', last_at: '2026-01-01T00:00:00Z',
                   receiver_id: 'recA', shots: 1, presses: { recA: 1 } } },
        { id: 'slacs-2', type: 'box', x: 100, y: 100, w: 20, h: 20, color: '#a0a6ae', text: 'SLACS_ROI #2',
          slacs: { kind: 'laser_shot', version: 1, isolation: 2, press: 2, target_px: [110, 110], side_um: 10,
                   rotation_deg: 0, calibrated: true, at: '2026-01-01T00:00:01Z', last_at: '2026-01-01T00:00:01Z',
                   receiver_id: 'recA', shots: 1, presses: { recA: 2 } } },
      ],
    };
    const okImportSlacs = await c.openAnnotationFile(fakeFile('sample.annotations.json', slacsExport));
    const afterSlacsImport = JSON.parse(JSON.stringify(c.state.annLaserState));

    // Fire a new laser shot far from #2: must become SLACS_ROI #6, NEVER reusing deleted #3/#4/#5!
    const owner = {
      sid: 's1', g: 'g1', p: null, ready: true, locked: false, loadFailed: false, quitPreparing: false,
      width: 2000, height: 2000, items: c.state.annotations, meta: c.state.annLaserState,
      add(item) { c.state.annLaserState = this.meta; c.state.annotations.push(item); return item; },
      update(oldItem, nextItem) { c.state.annLaserState = this.meta; c.state.annotations[c.state.annotations.indexOf(oldItem)] = nextItem; },
    };
    const shotOutcome = c.window.Nd2SlacsLaser.receive({
      sid: 's1', g: 'g1', p: null, token: 'tok1', press: 12, receiver_id: 'recA',
      at: '2026-01-01T00:05:00Z', slacs_session: 'sess1', target_px: [500, 500],
      target_corners_px: [[490, 490], [510, 490], [510, 510], [490, 510]],
      pixel_size_um: [0.5, 0.5], target_side_um: 10, target_rotation_deg: 0, target_calibrated: true,
    }, owner);

    // 2. Append-import GeoJSON carrying higher slacs state: merges highwater and presses.
    const geoAppend = {
      type: 'FeatureCollection',
      source: { name: 'sample.nd2', width: 2000, height: 2000 },
      coordinate_space: 'level-0-pixels',
      selection: { t: 0, p: 0, z: 0 },
      slacs: { isolation_highwater: 9, presses: { recA: 4, recB: 7 } },
      features: [{
        type: 'Feature',
        geometry: { type: 'Point', coordinates: [300, 300] },
        properties: { name: 'landmark' },
      }],
    };
    const okAppend = await c.openAnnotationFile(fakeFile('extra.geojson', geoAppend));
    const afterGeoAppend = JSON.parse(JSON.stringify(c.state.annLaserState));

    // 3. Replace-import a clean manual annotation file: high-water mark NEVER resets or lowers!
    const manualClean = {
      format: 'nd2wsi-annotations/2',
      coordinate_space: 'level-0-pixels',
      source: { name: 'sample.nd2', width: 2000, height: 2000 },
      selection: { t: 0, p: 0, z: 0 },
      items: [{ id: 'm1', type: 'pin', x: 50, y: 60, color: '#ff5555', text: 'cell' }],
    };
    const okClean = await c.openAnnotationFile(fakeFile('clean.annotations.json', manualClean));
    const afterCleanDoc = c.annotationDocument();

    return {
      okImportSlacs,
      afterSlacsImport,
      nextRoiLabel: shotOutcome.item.text,
      afterGeoAppend,
      okAppend,
      okClean,
      afterCleanLaserState: c.state.annLaserState,
      cleanDocHasSlacs: Object.prototype.hasOwnProperty.call(afterCleanDoc, 'slacs'),
      cleanDocSlacs: afterCleanDoc.slacs,
      saves,
    };
    """)
    assert result["okImportSlacs"] is True
    assert result["afterSlacsImport"] == {"isolation_highwater": 5, "presses": {"recA": 11}}
    assert result["nextRoiLabel"] == "SLACS_ROI #6"
    assert result["okAppend"] is True
    assert result["afterGeoAppend"] == {"isolation_highwater": 9, "presses": {"recA": 12, "recB": 7}}
    assert result["okClean"] is True
    assert result["afterCleanLaserState"] == {"isolation_highwater": 9, "presses": {"recA": 12, "recB": 7}}
    assert result["cleanDocHasSlacs"] is True
    assert result["cleanDocSlacs"] == {"isolation_highwater": 9, "presses": {"recA": 12, "recB": 7}}
    assert result["saves"] == 3


def test_b4_sw3_replace_open_never_lowers_laser_highwater():
    """B4: replace-opening older backups, bare arrays, official files, or bad slacs never lowers mark #5."""
    result = _run_export(r"""
    const f = fixture(), c = f.c;
    const staticDir = path.dirname(process.argv[1]);
    c.window.Nd2SlacsLaser = require(path.join(staticDir, 'slacs-laser-v1.js'));
    c.window.Nd2AnnotationGeoJSON = require(path.join(staticDir, 'annotation-geojson-v1.js'));
    c.window.Nd2AnnotationStore = require(path.join(staticDir, 'annotation-store-v1.js'));
    c.Blob = Blob;
    c.annCounter = 1;
    c.state.plate = false;
    c.state.info = { name: 'sample.nd2', width: 2000, height: 2000, selection: { t: 0, p: 0, z: 0 } };
    c.state.annContext = 'ctx-1';
    c.state.annSite = null;
    c.state.annRevision = 'rev-1';
    c.state.annForeignItems = [];
    c.pixelSize = () => [0.5, 0.5];
    c.annotationsChanged = () => {};
    c.setAnnStatus = () => {};
    c.$('ann-file').value = 'picked';

    vm.runInContext([
      production('currentSelection'),
      production('normalizedSelection'),
      production('selectionMatchesCurrent'),
      production('normalizedLaserState'),
      production('mergeLaserStates'),
      production('annotationItemsForThisSlide'),
      production('annotationDocument'),
      production('openAnnotationFile', true),
    ].join('\n'), c);

    function fakeFile(name, obj) {
      const text = JSON.stringify(obj);
      return { name, size: Buffer.byteLength(text, 'utf8'), text: async () => text };
    }

    const baseDoc = {
      format: 'nd2wsi-annotations/2',
      coordinate_space: 'level-0-pixels',
      source: { name: 'sample.nd2', width: 2000, height: 2000 },
      selection: { t: 0, p: 0, z: 0 },
      items: [{ id: 'm1', type: 'pin', x: 50, y: 60, color: '#ff5555', text: 'cell' }],
    };
    const scenarios = [
      ['lowerBackup', 'backup.annotations.json', { ...baseDoc, slacs: { isolation_highwater: 3, presses: { recA: 2 } } }],
      ['officialNoSlacs', 'official.annotations.json', baseDoc],
      ['bareArray', 'legacy-array.annotations.json', [{ id: 'arr-1', type: 'pin', x: 75, y: 85, color: '#ff5555', text: 'cell' }]],
      ['negativeSlacs', 'neg.annotations.json', { ...baseDoc, slacs: { isolation_highwater: -4, presses: { recA: -1 } } }],
      ['nullSlacs', 'null.annotations.json', { ...baseDoc, slacs: null }],
      ['overflowSlacs', 'huge.annotations.json', { ...baseDoc, slacs: { isolation_highwater: 1e30 } }],
    ];

    const outcomes = {};
    for (const [label, fname, payload] of scenarios) {
      c.state.annotations = [];
      c.state.annLaserState = { isolation_highwater: 5, presses: { recA: 11 } };
      const ok = await c.openAnnotationFile(fakeFile(fname, payload));
      const highwaterAfterOpen = c.state.annLaserState?.isolation_highwater;
      const owner = {
        sid: 's1', g: 'g1', p: null, ready: true, locked: false, loadFailed: false, quitPreparing: false,
        width: 2000, height: 2000, items: c.state.annotations, meta: c.state.annLaserState,
        add(item) { c.state.annLaserState = this.meta; c.state.annotations.push(item); return item; },
        update(oldItem, nextItem) { c.state.annLaserState = this.meta; c.state.annotations[c.state.annotations.indexOf(oldItem)] = nextItem; },
      };
      const shot = c.window.Nd2SlacsLaser.receive({
        sid: 's1', g: 'g1', p: null, token: 'tok-' + label, press: 12, receiver_id: 'recA',
        at: '2026-01-01T00:05:00Z', slacs_session: 'sess1', target_px: [500, 500],
        target_corners_px: [[490, 490], [510, 490], [510, 510], [490, 510]],
        pixel_size_um: [0.5, 0.5], target_side_um: 10, target_rotation_deg: 0, target_calibrated: true,
      }, owner);
      outcomes[label] = { ok, highwaterAfterOpen, nextText: shot.item.text };
    }
    return outcomes;
    """)
    for label, entry in result.items():
        assert entry["ok"] is True, label
        assert entry["highwaterAfterOpen"] == 5, label
        assert entry["nextText"] == "SLACS_ROI #6", label


def test_sw3_and_sua3_confocal_legacy_site_selection_restores_slacs_highwater():
    """Importing a 2.6.1/SLACS confocal export with selection:{p:0} on plane (t=0,p=0,z=2) succeeds and restores slacs."""
    result = _run_export(r"""
    const f = fixture(), c = f.c;
    const staticDir = path.dirname(process.argv[1]);
    c.window.Nd2SlacsLaser = require(path.join(staticDir, 'slacs-laser-v1.js'));
    c.window.Nd2AnnotationGeoJSON = require(path.join(staticDir, 'annotation-geojson-v1.js'));
    c.window.Nd2AnnotationStore = require(path.join(staticDir, 'annotation-store-v1.js'));
    c.Blob = Blob;
    c.annCounter = 1;
    c.state.plate = { focus: 0 };
    c.state.info = {
      name: 'confocal.czi',
      width: 1000,
      height: 1000,
      plate: { annotationScope: 'plane' },
      selection: { t: 0, p: 0, z: 'mid', z_resolved: 2 },
    };
    c.state.annContext = 'ctx-1';
    c.state.annSite = 0;
    c.state.annRevision = 'rev-1';
    c.state.annForeignItems = [];
    c.state.annotations = [];
    c.state.annLaserState = null;
    c.activeFrameParams = () => ({ t: 0, p: 0, z: 2 });
    c.annotationStore = { ownsFrame: () => true };
    c.pixelSize = () => [0.5, 0.5];
    c.annotationsChanged = () => {};
    c.setAnnStatus = () => {};
    c.$('ann-file').value = 'picked';

    vm.runInContext([
      production('currentSelection'),
      production('normalizedSelection'),
      production('selectionMatchesCurrent'),
      production('normalizedLaserState'),
      production('mergeLaserStates'),
      production('annotationItemsForThisSlide'),
      production('annotationDocument'),
      production('openAnnotationFile', true),
    ].join('\n'), c);

    const legacySlacsDoc = {
      format: 'nd2wsi-annotations/2',
      coordinate_space: 'level-0-pixels',
      source: { name: 'confocal.czi', width: 1000, height: 1000 },
      selection: { p: 0 },
      slacs: { isolation_highwater: 5, presses: { recConf: 9 } },
      items: [{ id: 's1', type: 'box', x: 10, y: 10, w: 20, h: 20, color: '#a0a6ae', text: 'SLACS_ROI #1',
        slacs: { kind: 'laser_shot', version: 1, isolation: 1, shots: 1 } }],
    };
    const text = JSON.stringify(legacySlacsDoc);
    const ok = await c.openAnnotationFile({ name: 'confocal.annotations.json', size: text.length, text: async () => text });
    return { ok, laserState: c.state.annLaserState, doc: c.annotationDocument() };
    """)
    assert result["ok"] is True
    assert result["laserState"] == {"isolation_highwater": 5, "presses": {"recConf": 9}}
    assert result["doc"]["selection"] == {"t": 0, "p": 0, "z": 2}
    assert result["doc"]["slacs"] == {"isolation_highwater": 5, "presses": {"recConf": 9}}


def test_sw3_annotation_workspace_write_preserves_slacs_metadata_when_omitted(tmp_path):
    session = SimpleNamespace(
        role="user",
        annotation_root=str(tmp_path / "annotations"),
        drafts_root=str(tmp_path / "drafts"),
        as_dict=lambda: {"id": "win-1", "role": "user"},
    )
    workspace = AnnotationWorkspace(session)
    sidecar = tmp_path / "sample.annotations.json"

    _, rev0 = workspace.read(sidecar)
    rev1 = workspace.write(
        sidecar,
        {
            "format": "nd2wsi-annotations/2",
            "items": [{"id": "s1", "type": "box", "x": 1, "y": 2, "w": 3, "h": 4}],
            "slacs": {"isolation_highwater": 4, "presses": {"rec1": 4}},
            "vendor_extension": {"keep": True},
        },
        rev0,
    )
    first_doc, _ = workspace.read(sidecar)
    assert first_doc["slacs"] == {"isolation_highwater": 4, "presses": {"rec1": 4}}

    # B4: omitting "slacs" on save must preserve existing "slacs" high-water metadata on disk.
    workspace.write(
        sidecar,
        {
            "format": "nd2wsi-annotations/2",
            "items": [{"id": "m1", "type": "pin", "x": 10, "y": 20}],
        },
        rev1,
    )
    second_doc, _ = workspace.read(sidecar)
    assert second_doc["slacs"] == {"isolation_highwater": 4, "presses": {"rec1": 4}}
    assert second_doc["vendor_extension"] == {"keep": True}
    assert second_doc["items"] == [{"id": "m1", "type": "pin", "x": 10, "y": 20}]


# ---- SW-4: packaging/build_windows_local.ps1 ---------------------------------


def test_sw4_build_windows_local_ps1_builds_sdpc_codec_narrows_zip_and_forwards_webview2(tmp_path, monkeypatch):
    script = (REPO / "packaging" / "build_windows_local.ps1").read_text(encoding="utf-8")
    assert "[string]$OutputZip" in script
    assert "[string]$SdpcCodecManifest" in script
    assert "[string]$MsysBash" in script
    assert "[string]$WebView2FixedRuntime" in script
    assert "'ND2WSI_SDPC_CODEC_MANIFEST'" in script
    assert "'ND2WSI_WEBVIEW2_RUNTIME'" in script

    preflight_idx = script.index("'packaging/build_sdpc_codec.py', '--preflight'")
    pytest_unit_idx = script.index("Invoke-Checked $python @('-m', 'pytest', 'tests', '-q', '-m', 'not realdata')")
    pytest_real_idx = script.index("Invoke-Checked $python @('-m', 'pytest', 'tests', '-q', '-m', 'realdata')")
    build_codec_idx = script.index("'packaging/build_sdpc_codec.py', '--output'")
    verify_wheel_idx = script.index("'packaging/verify_sdpc_codec.py', '--manifest', $codecManifest, '--wheel-only'")
    install_wheel_idx = script.index("'--no-deps', '--reinstall', $codecWheel")
    verify_runtime_idx = script.index("Invoke-Checked $python @('packaging/verify_sdpc_codec.py', '--manifest', $codecManifest)")
    webview2_forced_idx = script.index("$env:ND2WSI_WEBVIEW2_RUNTIME = 'bundled'")
    build_args_idx = script.index("'packaging/build_windows.py'")
    invoke_build_win_idx = script.index("Invoke-Checked $python $buildArgs")
    assert (
        preflight_idx
        < pytest_unit_idx
        < pytest_real_idx
        < build_codec_idx
        < verify_wheel_idx
        < install_wheel_idx
        < verify_runtime_idx
        < build_args_idx
        < webview2_forced_idx
        < invoke_build_win_idx
    )
    assert "--source-cache" in script
    assert "$env:ND2WSI_SDPC_CODEC_MANIFEST = $codecManifest" in script
    assert "Get-ChildItem -LiteralPath $output -Filter 'nd2wsi-viewer-*-windows-x64.zip'" in script
    assert "Get-ChildItem -LiteralPath $output -Filter '*.zip'" not in script
    assert "sdpc-codec-sources-windows-x64.zip" in script
    assert "sdpc-codec-sources-windows-x64.zip.sha256" in script
    assert "--webview2-fixed-runtime" in script
    assert "-RequireBundledWebView2" in script
    assert script.count("'packaging/build_sdpc_codec.py', '--output'") == 1

    # CLI contract cross-check: every --flag passed to packaging/*.py must exist in --help
    for match in re.finditer(r"@\('packaging/([a-z_]+\.py)'([^)]*)\)", script):
        py_script = match.group(1)
        used = set(re.findall(r"'(--[a-z0-9-]+)'", match.group(2)))
        help_out = subprocess.run(
            [sys.executable, f"packaging/{py_script}", "--help"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        known = set(re.findall(r"(?<![\w-])(--[a-z0-9][a-z0-9-]*)", help_out))
        assert used <= known, f"{py_script}: unknown flags {used - known}"
    build_win_help = subprocess.run(
        [sys.executable, "packaging/build_windows.py", "--help"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "--webview2-fixed-runtime" in build_win_help

    # Every $env:VAR assignment must be inside try{}, saved in $environmentBefore, and restored in finally
    saved_block = re.search(r"foreach \(\$name in @\(([^)]*)\)\) \{\s*\$environmentBefore", script).group(1)
    saved_names = set(re.findall(r"'([A-Z0-9_]+)'", saved_block))
    assigned = set(re.findall(r"\$env:([A-Z0-9_]+)\s*=", script))
    assert assigned <= saved_names
    try_at, finally_at = script.find("\ntry {"), script.find("\nfinally {")
    restore_at = script.find("[Environment]::SetEnvironmentVariable($name, $environmentBefore[$name], 'Process')")
    assert 0 < try_at < finally_at < restore_at
    assert all(try_at < m.start() < finally_at for m in re.finditer(r"\$env:[A-Z0-9_]+\s*=", script))

    # Unit-test preflight_check
    spec = importlib.util.spec_from_file_location("build_sdpc_codec", REPO / "packaging" / "build_sdpc_codec.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    msys_usr_bin = tmp_path / "msys64" / "usr" / "bin"
    msys_usr_bin.mkdir(parents=True)
    bash = msys_usr_bin / "bash.exe"
    bash.write_text("", encoding="utf-8")
    monkeypatch.setattr(builder.platform, "system", lambda: "Windows")
    monkeypatch.setattr(builder.platform, "machine", lambda: "ARM64")
    with pytest.raises(SystemExit, match="ND2WSI_SDPC_CODEC_MANIFEST"):
        builder.preflight_check(str(bash))


# ---- SW-5: translucent-v1.js synchronous renderLaserItems on Follow jog ------


def _run_translucent_js(body: str):
    if test_translucent_js.NODE is None:
        pytest.skip("node is not installed")
    return test_translucent_js.run(body)


def test_sw5_consume_stage_state_moves_laser_annotations_synchronously_with_roi_overlay():
    result = _run_translucent_js(r"""
      const h = followHarness({
        initialContext: { rotation: 0, flip: false, rois: [{ x: 400, y: 250, w: 20, h: 10 }], roiIndex: 0 },
      });
      const m = h.mounted;
      await m.pollSlacsDiscovery();
      await m.startStageFollow();
      await m.toggleLock();

      const item = {
        id: 'slacs-laser-r1-1',
        type: 'box',
        x: 410,
        y: 260,
        w: 10,
        h: 20,
        text: 'SLACS_ROI #1',
        slacs: { kind: 'laser_shot', shots: 1 },
      };
      h.win.nd2TranslucentLaserItem(item);

      const roiLayer = h.element('roi-overlay');
      const laserLayer = h.element('laser-annotations');
      const roiBefore = roiLayer.children[0].attributes.points;
      const laserBefore = laserLayer.children[0].attributes.points;

      // Jog stage via pollStageFollow WITHOUT firing viewer update-viewport / rAF.
      h.snapshot.status = 'following';
      h.snapshot.seq = 1;
      h.snapshot.displacement_um = [10, -20];
      await m.pollStageFollow();

      const roiAfter = roiLayer.children[0].attributes.points;
      const laserAfter = laserLayer.children[0].attributes.points;
      return { roiBefore, roiAfter, laserBefore, laserAfter };
    """)
    assert result["roiBefore"] != result["roiAfter"]
    assert result["laserBefore"] != result["laserAfter"]

    def first_vertex(points_str: str) -> list[float]:
        return [float(v) for v in points_str.split()[0].split(",")]

    roi_dx = first_vertex(result["roiAfter"])[0] - first_vertex(result["roiBefore"])[0]
    roi_dy = first_vertex(result["roiAfter"])[1] - first_vertex(result["roiBefore"])[1]
    laser_dx = first_vertex(result["laserAfter"])[0] - first_vertex(result["laserBefore"])[0]
    laser_dy = first_vertex(result["laserAfter"])[1] - first_vertex(result["laserBefore"])[1]
    assert laser_dx == pytest.approx(roi_dx)
    assert laser_dy == pytest.approx(roi_dy)


def test_c12_sw5_skips_cleanly_when_node_is_missing(monkeypatch):
    """C12: SW-5 translucent runner must raise pytest.skip instead of TypeError when Node is absent."""
    monkeypatch.setattr(test_translucent_js, "NODE", None)
    with pytest.raises(pytest.skip.Exception, match="node is not installed"):
        test_sw5_consume_stage_state_moves_laser_annotations_synchronously_with_roi_overlay()
