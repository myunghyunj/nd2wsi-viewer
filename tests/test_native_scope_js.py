"""Executed geometry checks for native trackpad stage scoping."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = (
    Path(__file__).resolve().parents[1]
    / "nd2wsi"
    / "static"
    / "native-scope-v1.js"
)
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SCRIPT = r"""
const Scope = require(process.argv[1]);
const spec = JSON.parse(process.argv[2]);
let value;
if (spec.fn === "map") value = Scope.mapRect(spec.local, spec.frame, spec.viewport);
else if (spec.fn === "build") value = Scope.buildScopes(spec.entries, spec.viewport);
else if (spec.fn === "roundtrip") {
  const point = Scope.pointFromInput(spec.input, spec.viewport);
  value = {point, local: Scope.pointInFrame(point, spec.frame)};
} else if (spec.fn === "normalize") value = Scope.normalizeRect(spec.rect, spec.viewport);
process.stdout.write(JSON.stringify(value));
"""


def _run(spec):
    result = subprocess.run(
        [NODE, "-e", SCRIPT, str(MODULE), json.dumps(spec)],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    return json.loads(result.stdout)


def test_iframe_rect_maps_to_normalized_shell_coordinates_with_css_scaling():
    mapped = _run(
        {
            "fn": "map",
            "local": {"left": 100, "top": 50, "right": 900, "bottom": 450},
            "frame": {
                "left": 100,
                "top": 200,
                "right": 600,
                "bottom": 450,
                "clientWidth": 1000,
                "clientHeight": 500,
            },
            "viewport": {"width": 1000, "height": 500},
        }
    )

    assert mapped == {"left": 0.15, "top": 0.45, "right": 0.55, "bottom": 0.85}


def test_scope_builder_filters_hidden_and_invalid_panes_and_maps_exclusions():
    scopes = _run(
        {
            "fn": "build",
            "viewport": {"width": 1000, "height": 800},
            "entries": [
                {
                    "target": "11111111",
                    "visible": True,
                    "frame": {
                        "left": 0,
                        "top": 0,
                        "right": 500,
                        "bottom": 800,
                        "clientWidth": 500,
                        "clientHeight": 800,
                    },
                    "include": {"left": 10, "top": 100, "right": 490, "bottom": 790},
                    "exclude": [
                        {"left": 20, "top": 110, "right": 120, "bottom": 210}
                    ],
                },
                {
                    "target": "22222222",
                    "visible": False,
                    "frame": {"left": 500, "top": 0, "right": 1000, "bottom": 800},
                    "include": {"left": 0, "top": 0, "right": 500, "bottom": 800},
                },
                {"target": "33333333", "visible": True, "include": {}},
            ],
        }
    )

    assert scopes == [
        {
            "target": "11111111",
            "include": {"left": 0.01, "top": 0.125, "right": 0.49, "bottom": 0.9875},
            "exclude": [
                {"left": 0.02, "top": 0.1375, "right": 0.12, "bottom": 0.2625}
            ],
        }
    ]


def test_native_ratio_round_trips_to_the_captured_iframe_client_space():
    result = _run(
        {
            "fn": "roundtrip",
            "input": {"clientXRatio": 0.3, "clientYRatio": 0.4},
            "viewport": {"width": 1000, "height": 800},
            "frame": {
                "left": 100,
                "top": 120,
                "right": 600,
                "bottom": 520,
                "clientWidth": 1000,
                "clientHeight": 800,
            },
        }
    )

    assert result == {"point": {"x": 300, "y": 320}, "local": {"x": 400, "y": 400}}


def test_shell_blocker_rect_normalizes_and_clips_to_the_webview():
    normalized = _run(
        {
            "fn": "normalize",
            "rect": {"left": -20, "top": 100, "right": 120, "bottom": 250},
            "viewport": {"width": 1000, "height": 500},
        }
    )

    assert normalized == {"left": 0, "top": 0.2, "right": 0.12, "bottom": 0.5}
