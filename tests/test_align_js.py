"""The similarity fit behind linked slides, exercised through node.

The math lives in one classic-script module that the shell loads, so the
test drives that exact file rather than a Python port of it."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ALIGN = Path(__file__).resolve().parents[1] / "nd2wsi" / "static" / "align-v1.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SCRIPT = r"""
const A = require(process.argv[1]);
const cases = JSON.parse(process.argv[2]);
const out = {};
for (const c of cases) {
  const src = c.from.map(([x, y]) => ({x, y}));
  const dst = c.to.map(([x, y]) => ({x, y}));
  const fit = A.fitSimilarity(src, dst, c.options || {});
  if (!fit) { out[c.name] = null; continue; }
  const inv = A.invert(fit.transform);
  const back = inv ? A.apply(inv, A.apply(fit.transform, src[0])) : null;
  out[c.name] = {
    angle: fit.angleDeg, scale: fit.scale, rms: fit.rms, reflected: fit.reflected,
    mirrored: A.mirrored(fit.transform), pairs: fit.pairs,
    roundtrip: back ? Math.hypot(back.x - src[0].x, back.y - src[0].y) : null,
    display: A.displayPose(fit.transform),
  };
}
process.stdout.write(JSON.stringify(out));
"""

ORIENTATION_SCRIPT = r"""
const A = require(process.argv[1]);
const actions = ["rotate-left", "rotate-right", "flip-horizontal", "flip-vertical", "transpose"];
const I = A.identity();
const F = A.screenOperation("flip-horizontal");
const R = A.screenOperation("rotate-right");
const clean = (value) => Math.abs(value) < 1e-10 ? 0 : value;
const matrix = (t) => [t.a, t.b, t.c, t.d].map(clean);
const error = (a, b) => Math.max(...matrix(a).map((value, i) => Math.abs(value - matrix(b)[i])));
const rotation = (degrees) => {
  const radians = degrees * Math.PI / 180;
  return {
    a: Math.cos(radians), b: -Math.sin(radians),
    c: Math.sin(radians), d: Math.cos(radians), tx: 0, ty: 0,
  };
};
const screenFromPose = (pose) => A.compose(
  pose.flipped ? F : I,
  rotation(pose.degrees)
);

const orientations = [];
let turn = I;
for (let i = 0; i < 4; i += 1) {
  orientations.push(turn, A.compose(turn, F));
  turn = A.compose(R, turn);
}
let maxFormulaError = 0;
let maxDisplayError = 0;
let maxActionError = 0;
let maxCenterError = 0;
let maxSwapError = 0;
let cases = 0;
const anchorCenter = {x: 130, y: 70};
const memberCenter = {x: 410, y: 260};
for (const orientation of orientations) {
  for (const action of actions) {
    cases += 1;
    const operation = A.screenOperation(action);
    const expected = A.compose(orientation, A.invert(operation));
    const actual = A.reorient(orientation, action);
    maxFormulaError = Math.max(maxFormulaError, error(actual, expected));

    // OSD applies its pose as F * R. That display must cancel the stored
    // anchor-to-member orientation exactly, even for reflected quarter turns.
    const display = screenFromPose(A.displayPose(actual));
    maxDisplayError = Math.max(maxDisplayError, error(A.compose(display, actual), I));
    const previousDisplay = screenFromPose(A.displayPose(orientation));
    maxActionError = Math.max(
      maxActionError,
      error(display, A.compose(operation, previousDisplay))
    );

    const centered = A.translationMatching(actual, anchorCenter, memberCenter);
    const mappedCenter = A.apply(centered, anchorCenter);
    maxCenterError = Math.max(
      maxCenterError,
      Math.hypot(mappedCenter.x - memberCenter.x, mappedCenter.y - memberCenter.y)
    );

    const swapped = A.invert(actual);
    const swappedDisplay = screenFromPose(A.displayPose(swapped));
    maxSwapError = Math.max(maxSwapError, error(A.compose(swappedDisplay, swapped), I));
  }
}

// A reflected non-quarter-turn checks the same F * R inverse rule beyond D4.
const theta = 37 * Math.PI / 180;
const scale = 1.7;
const arbitrary = {
  a: -scale * Math.cos(theta), b: -scale * Math.sin(theta),
  c: -scale * Math.sin(theta), d: scale * Math.cos(theta), tx: 25, ty: -9,
};
const arbitraryDisplay = screenFromPose(A.displayPose(arbitrary));
const scaledIdentity = {a: scale, b: 0, c: 0, d: scale, tx: 0, ty: 0};
const arbitraryError = error(A.compose(arbitraryDisplay, {...arbitrary, tx: 0, ty: 0}), scaledIdentity);
const arbitraryInverse = A.invert(arbitrary);
const inverseDisplay = screenFromPose(A.displayPose(arbitraryInverse));
const inverseScaleIdentity = {a: 1 / scale, b: 0, c: 0, d: 1 / scale, tx: 0, ty: 0};
const inverseError = error(
  A.compose(inverseDisplay, {...arbitraryInverse, tx: 0, ty: 0}),
  inverseScaleIdentity
);

process.stdout.write(JSON.stringify({
  operations: Object.fromEntries(actions.map((action) => [action, matrix(A.screenOperation(action))])),
  cases,
  uniqueOrientations: new Set(orientations.map((value) => matrix(value).join(","))).size,
  maxFormulaError,
  maxDisplayError,
  maxActionError,
  maxCenterError,
  maxSwapError,
  arbitrary: {pose: A.displayPose(arbitrary), error: arbitraryError, inverseError},
  transposePose: A.displayPose(A.screenOperation("transpose")),
  reset: matrix(A.reorient(A.screenOperation("transpose"), "reset")),
  invalidOperation: A.screenOperation("diagonal-ish"),
  invalidReorient: A.reorient(I, "diagonal-ish"),
}));
"""


def _transform(angle_deg, scale, tx, ty, mirror=False):
    import math

    th = math.radians(angle_deg)
    fx = -1.0 if mirror else 1.0
    return {
        "a": scale * math.cos(th) * fx,
        "b": -scale * math.sin(th),
        "c": scale * math.sin(th) * fx,
        "d": scale * math.cos(th),
        "tx": tx,
        "ty": ty,
    }


def _apply(t, x, y):
    return [t["a"] * x + t["b"] * y + t["tx"], t["c"] * x + t["d"] * y + t["ty"]]


def _run(cases):
    result = subprocess.run(
        [NODE, "-e", SCRIPT, str(ALIGN), json.dumps(cases)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def _run_orientation():
    result = subprocess.run(
        [NODE, "-e", ORIENTATION_SCRIPT, str(ALIGN)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout)


SQUARE = [[0, 0], [1000, 0], [1000, 800], [0, 800]]


def test_fit_recovers_rotation_scale_translation_and_mirror():
    plain = _transform(37.0, 1.25, 500.0, -120.0)
    mirrored = _transform(-20.0, 0.8, 30.0, 70.0, mirror=True)
    out = _run(
        [
            {"name": "plain", "from": SQUARE, "to": [_apply(plain, *p) for p in SQUARE]},
            {"name": "mirror", "from": SQUARE, "to": [_apply(mirrored, *p) for p in SQUARE]},
        ]
    )
    assert out["plain"]["angle"] == pytest.approx(37.0, abs=1e-9)
    assert out["plain"]["scale"] == pytest.approx(1.25, abs=1e-9)
    assert out["plain"]["rms"] < 1e-9
    assert out["plain"]["reflected"] is False and out["plain"]["mirrored"] is False
    assert out["plain"]["roundtrip"] < 1e-9
    assert out["mirror"]["angle"] == pytest.approx(-20.0, abs=1e-9)
    assert out["mirror"]["scale"] == pytest.approx(0.8, abs=1e-9)
    assert out["mirror"]["reflected"] is True and out["mirror"]["mirrored"] is True
    assert out["mirror"]["rms"] < 1e-9


def test_fit_is_least_squares_over_noisy_pairs_and_reports_the_residual():
    truth = _transform(12.0, 1.0, 40.0, 40.0)
    noisy = [_apply(truth, *p) for p in SQUARE]
    noisy[1][0] += 6.0
    noisy[2][1] -= 6.0
    out = _run([{"name": "noisy", "from": SQUARE, "to": noisy}])
    assert out["noisy"]["angle"] == pytest.approx(12.0, abs=0.6)
    assert 2.0 < out["noisy"]["rms"] < 6.0
    assert out["noisy"]["pairs"] == 4


def test_fit_refuses_degenerate_input():
    out = _run(
        [
            {"name": "one", "from": [[0, 0]], "to": [[5, 5]]},
            {"name": "same", "from": [[3, 3], [3, 3]], "to": [[0, 0], [1, 1]]},
            {"name": "nan", "from": [[0, 0], [1, 1]], "to": [[0, 0], [None, 1]]},
        ]
    )
    assert out == {"one": None, "same": None, "nan": None}


def test_reflection_can_be_forced_either_way():
    truth = _transform(5.0, 1.0, 0.0, 0.0)
    to = [_apply(truth, *p) for p in SQUARE]
    out = _run(
        [
            {"name": "forbid", "from": SQUARE, "to": to, "options": {"reflection": False}},
            {"name": "force", "from": SQUARE, "to": to, "options": {"reflection": True}},
        ]
    )
    assert out["forbid"]["reflected"] is False and out["forbid"]["rms"] < 1e-9
    assert out["force"]["reflected"] is True and out["force"]["rms"] > 100


def test_screen_operations_are_exact_in_clockwise_y_down_coordinates():
    out = _run_orientation()
    assert out["operations"] == {
        "rotate-left": [0, 1, -1, 0],
        "rotate-right": [0, -1, 1, 0],
        "flip-horizontal": [-1, 0, 0, 1],
        "flip-vertical": [1, 0, 0, -1],
        "transpose": [0, 1, 1, 0],
    }
    assert out["transposePose"] == {"degrees": 90, "flipped": True}
    assert out["reset"] == [1, 0, 0, 1]
    assert out["invalidOperation"] is None
    assert out["invalidReorient"] is None


def test_every_d4_orientation_composes_and_recenters_without_coordinate_drift():
    out = _run_orientation()
    assert out["uniqueOrientations"] == 8
    assert out["cases"] == 8 * 5
    assert out["maxFormulaError"] < 1e-10
    assert out["maxActionError"] < 1e-10
    assert out["maxCenterError"] < 1e-10


def test_display_pose_is_the_osd_fr_inverse_for_reflections_and_swaps():
    out = _run_orientation()
    assert out["maxDisplayError"] < 1e-10
    assert out["maxSwapError"] < 1e-10
    assert out["arbitrary"]["pose"]["degrees"] == pytest.approx(-37.0, abs=1e-10)
    assert out["arbitrary"]["pose"]["flipped"] is True
    assert out["arbitrary"]["error"] < 1e-10
    assert out["arbitrary"]["inverseError"] < 1e-10
