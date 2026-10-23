"""AstroMapper imports use an explicit coordinate grid and retain pixel bins."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi" / "static"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")
HEADER = "INDEX\tX1\tY1\tX2\tY2\tNOTE\n"


def run(script):
    code = "const api=require(process.argv[1]);\n" + script
    result = subprocess.run([NODE, "-e", code, str(STATIC / "astromap-import-v1.js")],
                            check=True, capture_output=True, text=True, encoding="utf-8", timeout=20)
    return json.loads(result.stdout)


def test_tsv_bom_crlf_unicode_notes_and_original_indices_survive_identity_import():
    text = "\ufeff\r\n" + HEADER.replace("\n", "\r\n") + "7\t5\t6\t16\t14\t조직 A 💛\r\n19\t0\t0\t0\t0\t\r\n"
    out = run(f"""
const parsed=api.parse({json.dumps(text)}), before=JSON.stringify(parsed);
const mapped=api.mapToSource(parsed.rows,{{width:80,height:60}},{{width:80,height:60}});
process.stdout.write(JSON.stringify({{parsed,mapped,unchanged:before===JSON.stringify(parsed)}}));
""")
    assert out["parsed"]["count"] == 2 and out["unchanged"]
    assert out["mapped"] == [
        {"type": "box", "x": 5, "y": 6, "w": 12, "h": 9, "text": "조직 A 💛",
         "astromap": True, "astromapEnabled": True, "astromapImportIndex": 7},
        {"type": "box", "x": 0, "y": 0, "w": 1, "h": 1, "text": "",
         "astromap": True, "astromapEnabled": True, "astromapImportIndex": 19},
    ]


@pytest.mark.parametrize("body", [
    "", "1\t1\t2\t3\t4", "1\t1\t2\t3\t4\tA\textra",
    "0\t1\t2\t3\t4\tA", "1\t-1\t2\t3\t4\tA", "1\tNaN\t2\t3\t4\tA",
    "1\tInfinity\t2\t3\t4\tA", "1\t1.5\t2\t3\t4\tA", "1\t1e1\t2\t30\t40\tA",
    "1\t9007199254740993\t2\t9007199254740993\t4\tA",
    "1\t3\t2\t1\t4\tA", "1\t1\t4\t3\t2\tA",
    "1\t1\t2\t3\t4\tA\n01\t5\t6\t7\t8\tB",
    "1\t1\t2\t3\t4\t" + "x" * 4097,
])
def test_invalid_rows_fail_with_an_error_before_any_regions_are_returned(body):
    out = run(f"""
try {{api.parse({json.dumps(HEADER + body)});process.stdout.write('null');}}
catch(error) {{process.stdout.write(JSON.stringify(error.message));}}
""")
    assert isinstance(out, str) and out


@pytest.mark.parametrize("header", ["X1\tY1\tX2\tY2\tNOTE", "INDEX,X1,Y1,X2,Y2,NOTE", "INDEX\tY1\tX1\tX2\tY2\tNOTE"])
def test_unrecognized_headers_are_not_guessed(header):
    out = run(f"""
try {{api.parse({json.dumps(header + chr(10) + '1' + chr(9) + '1' + chr(9) + '2' + chr(9) + '3' + chr(9) + '4' + chr(9) + 'A')});process.stdout.write('null');}}
catch(error) {{process.stdout.write(JSON.stringify(error.message));}}
""")
    assert "header" in out


def test_import_limit_and_overlap_policy():
    out = run("""
const header='INDEX\\tX1\\tY1\\tX2\\tY2\\tNOTE\\n';
const text=header+Array.from({length:2000},(_,i)=>`${i+1}\\t1\\t2\\t3\\t4\\tA`).join('\\n');
const parsed=api.parse(text);
const mapped=api.mapToSource(parsed.rows,{width:80,height:60},{width:800,height:600});
let error;try{api.parse(text+'\\n2001\\t1\\t2\\t3\\t4\\tB');}catch(e){error=e.message;}
process.stdout.write(JSON.stringify({count:parsed.count,mapped:mapped.length,error}));
""")
    assert out["count"] == out["mapped"] == 2000
    assert "2000" in out["error"]


def test_200_valid_regions_preserve_indices_notes_and_exact_pixel_bounds():
    out = run(r"""
const text='INDEX\tX1\tY1\tX2\tY2\tNOTE\r\n'+Array.from({length:200},(_,i)=>
  `${i+1}\t${10+i}\t${20+i}\t${13+i}\t${25+i}\tROI ${i+1}`).join('\r\n');
const parsed=api.parse(text,{width:8000,height:5830}),before=JSON.stringify(parsed);
const mapped=api.mapToSource(parsed.rows,{width:8000,height:5830},{width:16000,height:11660});
process.stdout.write(JSON.stringify({count:parsed.count,unchanged:before===JSON.stringify(parsed),mapped}));
""")
    assert out["count"] == len(out["mapped"]) == 200 and out["unchanged"]
    for offset, region in enumerate(out["mapped"]):
        assert {key: region[key] for key in ("x", "y", "w", "h")} == {
            "x": (10 + offset) * 2, "y": (20 + offset) * 2, "w": 8, "h": 12,
        }
        assert region["astromapImportIndex"] == offset + 1
        assert region["text"] == f"ROI {offset + 1}"


def test_empty_roi_and_reference_mismatch_are_reported_together_without_normalization():
    out = run(r"""
const header='INDEX\tX1\tY1\tX2\tY2\tNOTE\n';
const lines=Array.from({length:200},(_,i)=>{
  const index=i+1;
  if(index===199)return '199\t4877\t8248\t4876\t8247\t199';
  const y=index>=58&&index<=182?5900:100;
  return `${index}\t100\t${y}\t110\t${y+10}\t${index}`;
});
const text=header+lines.join('\n');let error;
try {api.parse(text,{width:8000,height:5830});}catch(e){error=e.message;}
const nonempty=api.parse(header+lines.filter(line=>!line.startsWith('199\t')).join('\n'));
const before=JSON.stringify(nonempty.rows);let mappingError;
try {api.mapToSource(nonempty.rows,{width:8000,height:5830},{width:8000,height:5830});}
catch(e){mappingError=e.message;}
process.stdout.write(JSON.stringify({error,mappingError,unchanged:JSON.stringify(nonempty.rows)===before}));
""")
    assert "INDEX 199 has zero area (4877, 8248) → (4876, 8247)" in out["error"]
    for message in (out["error"], out["mappingError"]):
        assert "125 regions are outside" in message
        assert "8000 × 5830" in message and "first INDEX 58" in message
        assert "confirmed TXT coordinate dimensions" in message
    assert out["unchanged"]


def test_zero_area_is_distinct_from_reversed_endpoints_and_invalid_rows_still_count_toward_limit():
    out = run(r"""
const header='INDEX\tX1\tY1\tX2\tY2\tNOTE\n';
const messages=[];
for(const text of [header+'7\t4\t5\t3\t10\tzero width',
  header+'8\t4\t5\t2\t10\treversed',
  header+Array.from({length:2001},(_,i)=>`${i+1}\t4\t5\t3\t4\tempty`).join('\n')]) {
 try {api.parse(text);}catch(e){messages.push(e.message);}
}
process.stdout.write(JSON.stringify(messages));
""")
    assert "INDEX 7 has zero area" in out[0]
    assert "INDEX 8 has reversed endpoints" in out[1]
    assert "2000" in out[2]


def test_odd_dimensions_inclusive_endpoints_and_last_pixels_roundtrip_exactly():
    out = run("""
const exportApi=require(require('path').join(require('path').dirname(process.argv[1]),'astromap-v1.js'));
const rows=[{index:1,x1:2000,y1:1000,x2:2019,y2:1009,note:'A'},
 {index:2,x1:7999,y1:3999,x2:7999,y2:3999,note:'edge'}];
const mapped=api.mapToSource(rows,{width:8000,height:4000},{width:40001,height:20003});
const repeated=mapped.map(r=>exportApi.scaledCoordinates(r,40001,20003,8000,4000));
process.stdout.write(JSON.stringify({mapped,repeated}));
""")
    first, last = out["mapped"]
    assert {k: first[k] for k in ("x", "y", "w", "h")} == {"x": 10001, "y": 5001, "w": 100, "h": 50}
    assert last["x"] + last["w"] == 40001
    assert last["y"] + last["h"] == 20003
    assert out["repeated"] == [
        {"x1": 2000, "y1": 1000, "x2": 2019, "y2": 1009},
        {"x1": 7999, "y1": 3999, "x2": 7999, "y2": 3999},
    ]


def test_quantized_import_does_not_claim_to_recover_the_original_native_rectangle():
    out = run("""
const mapped=api.mapToSource([{index:1,x1:1,y1:1,x2:1,y2:1,note:'one JPG pixel'}],
 {width:1000,height:1000},{width:10000,height:10000})[0];
const smaller=api.mapToSource([{index:1,x1:5,y1:5,x2:5,y2:5,note:'legacy enlarged JPG'}],
 {width:1000,height:1000},{width:10,height:10})[0];
process.stdout.write(JSON.stringify({mapped,smaller}));
""")
    assert (out["mapped"]["x"], out["mapped"]["y"], out["mapped"]["w"], out["mapped"]["h"]) == (10, 10, 10, 10)
    assert (out["smaller"]["x"], out["smaller"]["y"], out["smaller"]["w"], out["smaller"]["h"]) == (0, 0, 1, 1)


def test_explicit_dimensions_bounds_and_matching_whole_image_are_required():
    out = run("""
const rows=[{index:1,x1:10,y1:20,x2:30,y2:40,note:'A'}];
const errors=[];
for(const [grid,source] of [[null,{width:100,height:100}],
 [{width:0,height:100},{width:100,height:100}],
 [{width:100.5,height:100},{width:100,height:100}],
 [{width:100,height:100},{width:200,height:100}],
 [{width:20,height:20},{width:200,height:200}],
 [{width:100,height:100},{width:NaN,height:100}]]) {
 try{api.mapToSource(rows,grid,source);errors.push(null);}catch(e){errors.push(e.message);}
}
process.stdout.write(JSON.stringify(errors));
""")
    assert all(isinstance(message, str) and message for message in out)
    assert "aspect" in out[3] and "outside" in out[4]


JPEG_SCRIPT = """
function frame(marker=0xc0,width=8000,height=4000) {
 return Uint8Array.from([0xff,marker,0,17,8,height>>8,height&255,width>>8,width&255,3,1,0x11,0,2,0x11,0,3,0x11,0]);
}
function blob(bytes) {
 const data=Uint8Array.from(bytes), reads=[];
 return {size:data.length,reads,slice(a,b){reads.push([a,b]);return {arrayBuffer:async()=>data.slice(a,b).buffer};}};
}
"""


@pytest.mark.parametrize("marker", [0xC0, 0xC2])
def test_jpeg_dimensions_read_without_decoding_image_pixels(marker):
    out = run(JPEG_SCRIPT + f"""
(async()=>{{
 const input=blob([0xff,0xd8,0xff,0xe0,0,6,1,2,3,4,...frame({marker}),0xff,0xda,0,8,0,0,0,0,0,0]);
 const dimensions=await api.readJpegSize(input);
 process.stdout.write(JSON.stringify({{dimensions,bytesRead:input.reads.reduce((n,[a,b])=>n+b-a,0),maxRead:Math.max(...input.reads.map(([a,b])=>b-a))}}));
}})();
""")
    assert out["dimensions"] == {"width": 8000, "height": 4000}
    assert out["bytesRead"] < 24 and out["maxRead"] <= 6


def test_jpeg_metadata_segments_are_skipped_instead_of_read_into_memory():
    out = run(JPEG_SCRIPT + """
(async()=>{
 const frameOffset=2+100*(65535+2), size=frameOffset+frame().length+4;
 const reads=[];
 const input={size,slice(a,b){
   reads.push([a,b]);
   const out=new Uint8Array(b-a);
   for(let i=a;i<b;i++) {
     if(i===0)out[i-a]=255;else if(i===1)out[i-a]=216;
     else if(i>=frameOffset)out[i-a]=frame()[i-frameOffset]||0;
     else {const offset=(i-2)%65537;out[i-a]=offset===0?255:offset===1?225:offset===2||offset===3?255:0;}
   }
   return {arrayBuffer:async()=>out.buffer};
 }};
 const dimensions=await api.readJpegSize(input);
 process.stdout.write(JSON.stringify({dimensions,size,bytesRead:reads.reduce((n,[a,b])=>n+b-a,0)}));
})();
""")
    assert out["dimensions"] == {"width": 8000, "height": 4000}
    assert out["size"] > 6_000_000 and out["bytesRead"] < 1024


def test_malformed_jpegs_fail_without_pixel_decode():
    out = run(JPEG_SCRIPT + """
(async()=>{
 const cases=[[1,2,3,4],[255,216,255,217],[255,216,255,224,0,30,1,2],
 [255,216,255,192,0,7,8,0,1,0,1],[255,216,...frame(0xc0,0,4000)],
 [255,216,255,218,0,8,1,2,3,4,5,6]];
 const errors=[];
 for(const bytes of cases){try{await api.readJpegSize(blob(bytes));errors.push(null);}catch(e){errors.push(e.message);}}
 process.stdout.write(JSON.stringify(errors));
})();
""")
    assert all(isinstance(message, str) and message for message in out)


def test_png_and_jpeg_shared_header_reader_never_reads_image_data():
    out = run(JPEG_SCRIPT + """
(async()=>{
 const png=blob([137,80,78,71,13,10,26,10,0,0,0,13,73,72,68,82,
   0,0,31,64,0,0,15,160,8,2,0,0,0,0,0,0,0]);
 const jpeg=blob([255,216,...frame(),255,217]);
 const sizes=[await api.readImageSize(png),await api.readImageSize(jpeg)];
 const invalid=blob([137,80,78,71,13,10,26,10,0,0,0,13,0,0,0,0,...Array(17).fill(0)]);
 let error;try{await api.readImageSize(invalid);}catch(e){error=e.message;}
 process.stdout.write(JSON.stringify({sizes,pngReads:png.reads,error}));
})();
""")
    assert out["sizes"] == [{"width": 8000, "height": 4000}] * 2
    assert out["pngReads"] == [[0, 24]]
    assert "PNG" in out["error"]
