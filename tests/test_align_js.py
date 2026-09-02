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
    display: {degrees: -fit.angleDeg, flipped: A.mirrored(fit.transform)},
  };
}
process.stdout.write(JSON.stringify(out));
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
