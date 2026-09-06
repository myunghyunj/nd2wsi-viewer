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


def _evaluate_math(script):
    result = subprocess.run(
        [NODE, "-e", "const A = require(process.argv[1]);\n" + script, str(ALIGN)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def test_candidate_keeps_parity_without_fixing_rotation_or_scale():
    out = _evaluate_math(
        """
const points = [{x:0,y:0},{x:1000,y:0},{x:1000,y:800},{x:0,y:800}];
const theta = 37 * Math.PI / 180;
const T = {a:-1.7*Math.cos(theta), b:-1.7*Math.sin(theta),
           c:-1.7*Math.sin(theta), d:1.7*Math.cos(theta), tx:100,ty:20};
const dst = points.map(p => A.apply(T,p));
const previous = A.fitCandidate(points, dst, {reflection:'infer'});
const edit = A.fitCandidate(points, dst, {
  reflection:'keep', reflected:previous.fit.reflected
});
const shorter = A.fitCandidate(points.slice(0,1), dst.slice(0,1));
process.stdout.write(JSON.stringify({previous,edit,shorter}));
"""
    )
    assert out["previous"]["status"] == "valid"
    assert out["edit"]["status"] == "valid"
    assert out["edit"]["fit"]["reflected"] is True
    assert out["edit"]["fit"]["angleDeg"] == pytest.approx(37)
    assert out["edit"]["fit"]["scale"] == pytest.approx(1.7)
    assert out["edit"]["fit"]["rms"] < 1e-9
    assert out["shorter"]["status"] == "incomplete"
    assert out["shorter"]["fit"] is None


def test_two_fixed_parity_pairs_fit_math_but_do_not_pass_four_point_app_qc():
    out = _evaluate_math(
        """
const src = [{x:0,y:0},{x:5,y:9}];
const dst = [{x:40,y:10},{x:49,y:15}];
process.stdout.write(JSON.stringify({
  fixed:A.fitSimilarity(src,dst,{reflection:'keep',reflected:true}),
  inferred:A.fitSimilarity(src,dst,{reflection:'infer'}),
  candidate:A.fitCandidate(src,dst,{reflection:'keep',reflected:true}),
  mathematicalMinimum:A.fitCandidate(src,dst,{reflection:'keep',reflected:true,minPoints:2})
}));
"""
    )
    assert out["fixed"]["reflected"] is True
    assert out["fixed"]["rms"] < 1e-9
    assert out["inferred"] is None
    assert out["candidate"]["status"] == "incomplete"
    assert out["candidate"]["fit"] is None
    assert out["mathematicalMinimum"]["status"] == "valid"


def test_infer_rejects_collinear_and_nearly_collinear_points_not_local_coverage():
    out = _evaluate_math(
        """
const points = y => [{x:0,y:0},{x:1,y:0},{x:2,y},{x:3,y:0}];
const line = points(0), nearLine = points(1e-8);
const local = [{x:100,y:100},{x:100.001,y:100},
               {x:100.001,y:100.001},{x:100,y:100.001}];
process.stdout.write(JSON.stringify({
  line:A.fitCandidate(line,line,{reflection:'infer'}),
  nearLine:A.fitCandidate(nearLine,nearLine,{reflection:'infer'}),
  fixedLine:A.fitCandidate(line,line,{reflection:'keep',reflected:false}),
  local:A.fitCandidate(local,local,{reflection:'infer',
    sourceBounds:{width:10000,height:10000},targetBounds:{width:10000,height:10000}})
}));
"""
    )
    for name in ("line", "nearLine"):
        assert out[name]["status"] == "degenerate"
        assert out[name]["reason"] == "mirror-needs-noncollinear-landmarks"
        assert out[name]["fit"] is None
    assert out["fixedLine"]["status"] == "valid"
    assert out["local"]["status"] == "valid"
    assert out["local"]["fit"]["rms"] < 1e-10
    assert out["local"]["warnings"] == ["source-local-coverage", "target-local-coverage"]


def test_candidate_rejects_duplicates_nonfinite_and_unpaired_points():
    out = _evaluate_math(
        """
const p = [{x:0,y:0},{x:1,y:0},{x:1,y:1},{x:0,y:1}];
const duplicated = [p[0],p[0],p[2],p[3]];
const bad = [p[0],p[1],p[2],{x:Infinity,y:1}];
process.stdout.write(JSON.stringify({
  duplicate:A.fitCandidate(p,duplicated),
  bad:A.fitCandidate(p,bad),
  mismatch:A.fitCandidate(p,p.slice(1)),
  lowLevelMismatch:A.fitSimilarity(p,p.slice(1)),
  invalidPolicy:A.fitCandidate(p,p,{reflection:'anything'}),
  emptyResidual:A.residual(A.identity(),[],[]),
  mismatchResidual:A.residual(A.identity(),p,p.slice(1))
}));
"""
    )
    assert out["duplicate"]["reason"] == "duplicate-landmarks"
    assert out["duplicate"]["status"] == "degenerate"
    assert out["bad"]["reason"] == "nonfinite-landmarks"
    assert out["mismatch"]["status"] == "incomplete"
    assert out["mismatch"]["reason"] == "unpaired-landmarks"
    assert out["lowLevelMismatch"] is None
    assert out["invalidPolicy"]["status"] == "degenerate"
    assert out["emptyResidual"] is None
    assert out["mismatchResidual"] is None


def test_normalized_fit_is_stable_for_tiny_and_translated_local_point_sets():
    out = _evaluate_math(
        """
const cases = [];
for (const [origin,size] of [[0,1e-9],[1e9,0.001]]) {
  const p = [{x:origin,y:origin},{x:origin+size,y:origin},
             {x:origin+size,y:origin+size},{x:origin,y:origin+size}];
  cases.push(A.fitCandidate(p,p,{reflection:'infer'}));
}
process.stdout.write(JSON.stringify(cases));
"""
    )
    assert all(item["status"] == "valid" for item in out)
    assert all(item["fit"]["scale"] == pytest.approx(1) for item in out)
    assert all(item["fit"]["rms"] < 1e-10 for item in out)


def test_current_residual_tracks_offset_and_swap_in_member_units():
    out = _evaluate_math(
        """
const from = [{x:0,y:0},{x:10,y:0},{x:10,y:10},{x:0,y:10}];
const F = {a:0,b:-2,c:-2,d:0,tx:80,ty:140};
const to = from.map(p=>A.apply(F,p));
const E = A.compose({...A.identity(),tx:100},F);
const inverseFit = A.invert(F), inverseEffective = A.invert(E);
// Inverse offset is not simply -100: move it through F^-1's linear part.
const inverseOffset = A.compose(inverseEffective,F);
process.stdout.write(JSON.stringify({
  fit:A.residual(F,from,to), current:A.residual(E,from,to),
  reverseFit:A.residual(inverseFit,to,from),
  reverseCurrent:A.residual(inverseEffective,to,from), inverseOffset,
  composition:A.apply(A.compose(F,E),from[2]),
  sequential:A.apply(F,A.apply(E,from[2]))
}));
"""
    )
    assert out["fit"] == pytest.approx(0)
    assert out["current"] == pytest.approx(100)
    assert out["reverseFit"] == pytest.approx(0)
    assert out["reverseCurrent"] == pytest.approx(50)
    assert out["inverseOffset"]["tx"] == pytest.approx(0)
    assert out["inverseOffset"]["ty"] == pytest.approx(50)
    assert out["composition"] == out["sequential"]


def test_pixel_mapping_conjugates_physical_transform_and_translation():
    out = _evaluate_math(
        """
const turn = {...A.screenOperation('rotate-right'),tx:10,ty:20};
const anisotropy = {x:0.25,y:0.5};
const swapped = {x:0.5,y:0.25};
const same = A.pixelMapping(turn,anisotropy,anisotropy);
const swap = A.pixelMapping(turn,anisotropy,swapped);
process.stdout.write(JSON.stringify({same,swap,
  samePose:A.rendererPose(same), swapPose:A.rendererPose(swap),
  noCalibration:A.pixelMapping(turn,{x:null,y:null},anisotropy),
  zeroCalibration:A.pixelMapping(turn,{x:0,y:0.25},anisotropy)
}));
"""
    )
    assert out["same"] == {"a": 0, "b": -2, "c": 0.5, "d": 0, "tx": 40, "ty": 40}
    assert out["samePose"]["supported"] is False
    assert out["samePose"]["reason"] == "nonuniform-pixel-mapping"
    assert out["swap"] == {"a": 0, "b": -1, "c": 1, "d": 0, "tx": 20, "ty": 80}
    assert out["swapPose"]["supported"] is True
    assert out["swapPose"]["pose"] == {"degrees": -90, "flipped": False}
    assert out["noCalibration"] is None
    assert out["zeroCalibration"] is None


def test_renderer_checks_full_gram_matrix_not_only_determinant_or_basis_lengths():
    out = _evaluate_math(
        """
const theta = 37*Math.PI/180;
const rotation = {a:Math.cos(theta),b:-Math.sin(theta),
                  c:Math.sin(theta),d:Math.cos(theta),tx:0,ty:0};
const scaleA = {x:0.25,y:0.5};
const same37 = A.pixelMapping(rotation,scaleA,scaleA);
const reflected = A.compose(rotation,A.screenOperation('flip-horizontal'));
const reflected37 = A.pixelMapping(reflected,scaleA,scaleA);
const result = {};
for (const [name,mapping] of Object.entries({
  same37, reflected37,
  differentRatio:A.pixelMapping(A.identity(),scaleA,{x:0.5,y:0.5}),
  determinantOne:{a:2,b:0,c:0,d:0.5,tx:0,ty:0},
  equalLengthShear:{a:1,b:0.5,c:0,d:Math.sqrt(0.75),tx:0,ty:0},
  square:A.pixelMapping(reflected,{x:0.25,y:0.25},{x:0.66,y:0.66}),
  axisReflection:A.pixelMapping(A.screenOperation('flip-horizontal'),scaleA,scaleA),
  relative:A.pixelMapping(reflected,{x:1,y:1},{x:1,y:1})
})) result[name] = A.rendererPose(mapping);
process.stdout.write(JSON.stringify(result));
"""
    )
    for name in ("same37", "reflected37", "differentRatio", "determinantOne", "equalLengthShear"):
        assert out[name]["supported"] is False
        assert out[name]["pose"] is None
    for name in ("square", "axisReflection", "relative"):
        assert out[name]["supported"] is True
    assert out["square"]["pose"]["degrees"] == pytest.approx(-37)
    assert out["square"]["pose"]["flipped"] is True
    assert out["square"]["scale"] == pytest.approx(0.25 / 0.66)


def test_renderer_tolerance_is_explicit_roundoff_policy_and_scale_independent():
    out = _evaluate_math(
        """
const check = (size, distortion) => A.rendererPose({
  a:size,b:0,c:0,d:size*(1+distortion),tx:0,ty:0
});
process.stdout.write(JSON.stringify({
  tolerance:A.RENDERER_SIMILARITY_TOLERANCE,
  cases:[1e-100,1,1e100].map(size=>({
    roundoff:check(size,1e-12), reject:check(size,1e-7), onePercent:check(size,0.01)
  })),
  zero:A.rendererPose({a:0,b:0,c:0,d:0,tx:0,ty:0}),
  invalid:A.rendererPose(null)
}));
"""
    )
    assert out["tolerance"] == 1e-10
    for item in out["cases"]:
        assert item["roundoff"]["supported"] is True
        assert item["reject"]["supported"] is False
        assert item["onePercent"]["supported"] is False
    assert out["zero"]["supported"] is False
    assert out["invalid"]["supported"] is False
