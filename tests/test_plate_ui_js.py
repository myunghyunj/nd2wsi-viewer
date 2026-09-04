"""Well-name headers, exercised through the production JavaScript helper."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "nd2wsi" / "static" / "plate-ui-v1.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _run(script):
    result = subprocess.run(
        [NODE, "-e", script, str(MODULE)],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    return json.loads(result.stdout)


def test_parse_well_name_normalizes_letters_and_numeric_padding():
    out = _run(
        r"""
const {parseWellName}=require(process.argv[1]);
process.stdout.write(JSON.stringify([
  parseWellName("A01"),
  parseWellName(" p0016 "),
  parseWellName("AA7"),
  parseWellName("A00"),
  parseWellName("Site 1"),
  parseWellName(null),
]));
"""
    )
    assert out == [
        {"row": "A", "col": "1"},
        {"row": "P", "col": "16"},
        {"row": "AA", "col": "7"},
        None,
        None,
        None,
    ]


def test_reverse_x_sixteen_by_sixteen_headers_follow_visual_axes():
    out = _run(
        r"""
const {wellHeaders}=require(process.argv[1]);
const letters=Array.from({length:16},(_,i)=>String.fromCharCode(65+i));
const placed=[];
for(let row=0;row<16;row++) {
  for(let col=0;col<16;col++) {
    const wellCol=16-col;
    placed.push({name:letters[row]+String(wellCol).padStart(2,"0"),row,col});
  }
}
const transposed=placed.map((site)=>({name:site.name,row:site.col,col:site.row}));
process.stdout.write(JSON.stringify({
  normal:wellHeaders(placed,16,16),
  transposed:wellHeaders(transposed,16,16),
}));
"""
    )
    letters = [chr(65 + i) for i in range(16)]
    reverse_numbers = [str(i) for i in range(16, 0, -1)]
    assert out["normal"] == {"rows": letters, "cols": reverse_numbers}
    assert out["transposed"] == {"rows": reverse_numbers, "cols": letters}


def test_rotated_grid_is_inferred_without_assuming_stage_x_or_y():
    out = _run(
        r"""
const {wellHeaders}=require(process.argv[1]);
// A clockwise quarter turn: well numbers run down the visual rows and the
// lettered well rows run right-to-left across the visual columns.
const placed=[];
for(let number=1;number<=3;number++) {
  placed.push({name:"B"+number,row:number-1,col:0});
  placed.push({name:"A"+number,row:number-1,col:1});
}
process.stdout.write(JSON.stringify(wellHeaders(placed,3,2)));
"""
    )
    assert out == {"rows": ["1", "2", "3"], "cols": ["B", "A"]}


def test_invalid_or_inconsistent_labels_are_rejected():
    out = _run(
        r"""
const {wellHeaders}=require(process.argv[1]);
const invalid=[
  {name:"A01",row:0,col:0},
  {name:"Site 2",row:0,col:1},
];
const inconsistent=[
  {name:"A1",row:0,col:0},
  {name:"B2",row:0,col:1},
  {name:"A2",row:1,col:0},
  {name:"B1",row:1,col:1},
];
const duplicateCell=[
  {name:"A1",row:0,col:0},
  {name:"A2",row:0,col:0},
];
const missingTrack=[
  {name:"A1",row:0,col:0},
  {name:"A2",row:0,col:1},
];
process.stdout.write(JSON.stringify([
  wellHeaders(invalid,1,2),
  wellHeaders(inconsistent,2,2),
  wellHeaders(duplicateCell,1,2),
  wellHeaders(missingTrack,2,2),
]));
"""
    )
    assert out == [None, None, None, None]
