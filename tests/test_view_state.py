"""Display-only, source-bound state transfer without annotation/session adoption."""

import copy
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from nd2wsi import app, render, view_state, window_launch
from nd2wsi.window_sessions import create_window_session


@pytest.fixture
def display():
    return {"version": 1, "source_dimensions": [3000, 2000], "center": [1250.5, 901.25], "zoom": .75,
            "channels": [{"window": [4, 65536], "gamma": .1, "color": [255, 5, 20], "visible": False},
                         {"window": [30, 220], "gamma": 8, "color": [0, 0, 255], "visible": True}]}


@pytest.mark.skipif(os.name != "posix", reason="macOS private handoff uses POSIX ownership/modes")
def test_round_trip_private_source_bound_handoff(tmp_path, display):
    source = tmp_path / "source.nd2"
    source.write_bytes(b"isolated fixture")
    path = view_state.write_handoff(display, source=source, role="agent", root=tmp_path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert view_state.read_handoff(path, source=source, role="agent") == display
    with pytest.raises(ValueError, match="role"):
        view_state.read_handoff(path, source=source, role="user")
    source.write_bytes(b"changed source")
    with pytest.raises(ValueError, match="source"):
        view_state.read_handoff(path, source=source, role="agent")


@pytest.mark.skipif(os.name != "posix", reason="macOS private handoff uses POSIX ownership/modes")
def test_handoff_rejects_symlink_world_readable_and_oversize(tmp_path, display):
    source = tmp_path / "source.nd2"
    source.write_bytes(b"fixture")
    path = view_state.write_handoff(display, source=source, role="user", root=tmp_path)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(OSError):
        view_state.read_handoff(link, source=source, role="user")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        view_state.read_handoff(path, source=source, role="user")
    path.chmod(0o600)
    path.write_text(" " * (view_state.MAX_BYTES + 1))
    with pytest.raises(ValueError, match="bounded"):
        view_state.read_handoff(path, source=source, role="user")


@pytest.mark.parametrize("key,value", [("zoom", float("nan")), ("zoom", True), ("zoom", 0),
                                       ("center", [-1, 4]), ("source_dimensions", [2.5, 20]),
                                       ("version", True), ("channels", [])])
def test_invalid_state_is_rejected(display, key, value):
    display[key] = value
    with pytest.raises(ValueError):
        view_state.validate_view_state(display)


def test_dimensions_channel_count_and_visibility_are_validated(display):
    with pytest.raises(ValueError, match="dimensions"):
        view_state.validate_view_state(display, dimensions=[3, 4])
    with pytest.raises(ValueError, match="count"):
        view_state.validate_view_state(display, channel_count=1)
    display["channels"][0]["visible"] = 1
    with pytest.raises(ValueError, match="boolean"):
        view_state.validate_view_state(display)


def registered_api(tmp_path, display, role="agent"):
    source = tmp_path / "slide.nd2"
    source.write_bytes(b"fixture")
    attrs = {"nd2wsi": {"levels": [{"width": 3000, "height": 2000}]},
             "omero": {"channels": [{"color": "FF0000"}, {"color": "00FF00"}]}}
    st = SimpleNamespace(source_path=source, attrs=attrs, lock=threading.Lock())
    registry = SimpleNamespace(get=lambda sid: st if sid == "slide1" else None,
                               listing=lambda: [{"sid": "slide1"}])
    session = create_window_session(role, tmp_path / "sessions")
    api = app.Api(source, window_session=session)
    api._httpd = SimpleNamespace(registry=registry)
    return api, st


@pytest.mark.parametrize("role", ["agent", "user"])
@pytest.mark.skipif(os.name != "posix", reason="macOS handoff bridge verifies POSIX ownership")
def test_bridge_uses_registered_source_and_preserves_role(tmp_path, display, monkeypatch, role):
    monkeypatch.setattr(app.sys, "platform", "darwin")
    api, st = registered_api(tmp_path, display, role)
    launch = Mock(return_value={"ok": True})
    monkeypatch.setattr(window_launch, "launch_window", launch)
    assert api.open_in_metal("unregistered", display)["ok"] is False
    launch.assert_not_called()
    assert api.open_in_metal("slide1", display)["ok"]
    assert launch.call_args.args == (role,)
    args = launch.call_args.kwargs
    assert args["source"] == st.source_path and args["prefer_metal"] is True
    assert view_state.read_handoff(args["handoff_state"], source=st.source_path, role=role) == display


def test_browser_handoff_colors_are_runtime_only_and_source_matched(tmp_path, display):
    api, st = registered_api(tmp_path, display)
    attrs = st.attrs
    before = copy.deepcopy(attrs)
    api._handoff_source = st.source_path
    api._handoff_state = display
    api._prepare_handoff()
    assert attrs == before and st.attrs is not attrs
    assert [c["color"] for c in st.attrs["omero"]["channels"]] == ["FF0514", "0000FF"]
    assert api.initial_view_state("slide1") == display
    assert api.initial_view_state("unregistered") is None
    assert api._handoff_state is None


def test_explicit_all_hidden_does_not_change_legacy_default_channels():
    assert render.parse_channels(None, 2) == [0, 1]
    assert render.parse_channels("", 2) == [0, 1]
    assert render.parse_channels("none", 2) == []
    raw = np.ones((2, 2, 3), dtype=np.uint16) * 100
    image = render.composite(raw, [], [(0, 100)] * 2, [(255, 0, 0), (0, 0, 255)], False)
    assert np.count_nonzero(image) == 0


def test_child_opt_in_is_separate_safe_and_cannot_inherit_diagnostic(monkeypatch, tmp_path):
    monkeypatch.setattr(window_launch.sys, "platform", "darwin")
    monkeypatch.setattr(window_launch.sys, "frozen", True, raising=False)
    monkeypatch.setenv("ND2WSI_VIEWPORT_AUTOQUIT", "1")
    popen = Mock(return_value=SimpleNamespace(pid=32))
    monkeypatch.setattr(window_launch.subprocess, "Popen", popen)
    result = window_launch.launch_window("user", source=tmp_path / "s.nd2", prefer_metal=True,
                                         handoff_state=tmp_path / "view.json")
    command = popen.call_args.args[0]
    assert result["ok"] and command[1] == "--new-window"
    assert "--prefer-metal" in command and "--renderer" not in command
    assert "ND2WSI_VIEWPORT_AUTOQUIT" not in popen.call_args.kwargs["env"]
    assert os.environ["ND2WSI_VIEWPORT_AUTOQUIT"] == "1"


def test_browser_diagnostic_refuses_user_or_unisolated_source(tmp_path):
    args = [str(tmp_path / "outside.nd2"), "--browser-replay-report", str(tmp_path / "report.json"),
            "--benchmark-context", str(tmp_path / "context.json"),
            "--benchmark-test-root", str(tmp_path / "test")]
    assert app.main(args) == 2
    assert app.main(args + ["--agent-window"]) == 2


@pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")
def test_js_display_and_camera_round_trip(display):
    module = Path(__file__).resolve().parents[1] / "nd2wsi/static/view-state-v1.js"
    script = """
      const m=require(process.argv[1]), d=JSON.parse(process.argv[2]);
      let rect={x:0,y:0,width:3000,height:2000};
      const viewport={getRotation:()=>0,getFlip:()=>false,getBounds:()=>rect,
        getContainerSize:()=>({x:900,y:600}),viewportToImageRectangle:r=>r,
        imageToViewportRectangle:(x,y,width,height)=>({x,y,width,height}),fitBounds:r=>{rect=r;}};
      const state={info:{width:3000,height:2000,channels:[{window:{start:0,end:100}},{window:{start:0,end:100}}]},
        viewer:{viewport,forceRedraw(){}},luts:[],channels:[]};
      m.applyDisplay(state,d);m.applyCamera(state.viewer,d);
      process.stdout.write(JSON.stringify(m.capture(state)));
    """
    result = subprocess.run([shutil.which("node"), "-e", script, str(module), json.dumps(display)],
                             capture_output=True, text=True, check=True, timeout=10)
    assert json.loads(result.stdout) == display
