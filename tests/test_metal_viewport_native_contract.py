"""Call the native validators directly, without GUI, GPU, or source access."""
import copy
import ctypes
import itertools
import json
import platform
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def validate():
    library = Path(__file__).resolve().parents[1] / "nd2wsi/metal_viewport/libnd2wsi_viewport.dylib"
    if platform.system() != "Darwin" or not library.exists():
        pytest.skip("built macOS viewport required")
    function = ctypes.CDLL(str(library)).nd2wsi_viewport_validate_contract
    function.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    function.restype = ctypes.c_int
    return lambda metadata, state=None: function(json.dumps(metadata).encode(), None if state is None else json.dumps(state).encode())


def metadata():
    return {"dtype": "uint16", "width": 1024, "height": 513, "tile_size": 512,
            "levels": [{"path": "0", "width": 1024, "height": 513, "downsample": 1},
                       {"path": "1", "width": 512, "height": 256, "downsample": 2}],
            "channels": [{"label": "DAPI", "window": [0, 65535], "color": [0, 0, 255]}]}


def state():
    return {"version": 1, "source_dimensions": [1024, 513], "center": [123.5, 256], "zoom": .5,
            "channels": [{"window": [120, 65536], "gamma": 2.5, "color": [3, 73, 189], "visible": False}]}


def test_valid_metadata_and_lossless_initial_state(validate):
    assert validate(metadata()) == 0
    assert validate(metadata(), state()) == 0


@pytest.mark.parametrize("replaying,agent,own_window", list(itertools.product((0, 1), repeat=3)))
def test_replay_physical_input_filter_is_agent_and_window_scoped(validate, replaying, agent, own_window):
    library = Path(__file__).resolve().parents[1] / "nd2wsi/metal_viewport/libnd2wsi_viewport.dylib"
    function = ctypes.CDLL(str(library)).nd2wsi_viewport_replay_input_scope
    function.argtypes = [ctypes.c_int] * 3
    function.restype = ctypes.c_int
    assert bool(function(replaying, agent, own_window)) == bool(replaying and agent and own_window)


@pytest.mark.parametrize("key,value", [("width", 0), ("width", "1024"), ("width", True),
                                     ("height", 513.5), ("tile_size", 513), ("dtype", "uint8"),
                                     ("levels", []), ("channels", []), ("channels", [None]),
                                     ("channels", metadata()["channels"] * 9)])
def test_invalid_top_level_metadata(validate, key, value):
    meta = metadata()
    meta[key] = value
    assert validate(meta) == 1


@pytest.mark.parametrize("key,value", [("path", "../0"), ("path", "0"), ("width", 2048),
                                     ("height", 0), ("downsample", 1), ("downsample", float("inf"))])
def test_invalid_level_rejected_before_configure(validate, key, value):
    meta = metadata()
    meta["levels"][1][key] = value
    assert validate(meta) == 1


@pytest.mark.parametrize("value", [None, [], 1, "bad", {"window": "bad"},
                                  {"label": "x", "window": [4, 4], "color": [0, 0, 0]}])
def test_malformed_channel_never_reaches_objc_message_send(validate, value):
    meta = metadata()
    meta["channels"] = [value]
    assert validate(meta) == 1


@pytest.mark.parametrize("key,value", [("version", True), ("version", 2), ("source_dimensions", [100, 513]),
                                     ("center", [1025, 0]), ("center", [0, -1]), ("zoom", 0),
                                     ("zoom", 33), ("zoom", float("nan")), ("channels", [])])
def test_invalid_state_cannot_retarget_or_distort(validate, key, value):
    initial = state()
    initial[key] = value
    assert validate(metadata(), initial) == 2


@pytest.mark.parametrize("key,value", [("visible", 1), ("visible", "true"), ("gamma", 0),
                                     ("gamma", 11), ("window", [65536, 65536]),
                                     ("color", [0, 0, 256]), ("color", [0, 0]), ("window", [20, 10])])
def test_invalid_channel_state(validate, key, value):
    initial = copy.deepcopy(state())
    initial["channels"][0][key] = value
    assert validate(metadata(), initial) == 2
