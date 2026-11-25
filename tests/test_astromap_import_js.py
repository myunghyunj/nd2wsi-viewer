"""AstroMapper imports use an explicit coordinate grid and retain pixel bins."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from node_host import run_node

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi" / "static"
HEADER = "INDEX\tX1\tY1\tX2\tY2\tNOTE\n"


def run(script):
    code = "const api=require(process.argv[1]);\n" + script
    result = run_node(code, STATIC / "astromap-import-v1.js", check=True)
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


def test_review_keeps_valid_rows_and_reports_exact_screenshot_failure_without_changing_text():
    out = run(r"""
const header='INDEX\tX1\tY1\tX2\tY2\tNOTE\n';
const lines=Array.from({length:40},(_,i)=>`${i+1}\t10\t20\t11\t21\tROI ${i+1}`);
const raw='41\t5675\t2543\t5675\t2542\t41';
const text=header+lines.concat(raw,'42\t4\t5\t4\t5\t조직 <img src=x onerror=alert(1)>').join('\n');
const before=text, grid={width:8000,height:5677};
const reviewed=api.review(text,grid), repaired=api.review(text,grid,{repairZeroArea:true});
const reverted=api.review(text,grid);
process.stdout.write(JSON.stringify({reviewed,repaired,reverted,unchanged:text===before,
  mapped:api.mapToSource(repaired.rows,grid,grid)}));
""")
    assert out["unchanged"] and out["reverted"] == out["reviewed"]
    review, repair = out["reviewed"], out["repaired"]
    assert (review["count"], review["total"], review["skippedCount"], review["repairedCount"]) == (41, 42, 1, 0)
    assert [row["index"] for row in review["rows"]] == list(range(1, 41)) + [42]
    issue = review["issues"][0]
    assert (issue["line"], issue["index"], issue["repairable"], issue["repaired"]) == (42, 41, True, False)
    assert issue["raw"] == "41\t5675\t2543\t5675\t2542\t41"
    assert issue["original"] == {"index": 41, "x1": 5675, "y1": 2543, "x2": 5675, "y2": 2542, "note": "41"}
    assert "has zero area (5675, 2543) → (5675, 2542)" in issue["message"]
    assert (repair["count"], repair["total"], repair["skippedCount"], repair["repairedCount"]) == (42, 42, 0, 1)
    assert repair["issues"][0]["repaired"] is True
    assert repair["issues"][0]["original"] == issue["original"]
    assert repair["rows"][40] == {**issue["original"], "y2": 2543}
    assert {key: out["mapped"][40][key] for key in ("x", "y", "w", "h")} == {
        "x": 5675, "y": 2543, "w": 1, "h": 1,
    }
    assert out["mapped"][-1]["astromapImportIndex"] == 42
    assert out["mapped"][-1]["text"] == "조직 <img src=x onerror=alert(1)>"


def test_review_repairs_only_collapsed_dimensions_with_explicit_boolean_opt_in():
    out = run(r"""
const text='INDEX\tX1\tY1\tX2\tY2\tNOTE\n'+[
 '1\t4\t5\t3\t9\tzero width',
 '2\t4\t5\t8\t4\tzero height',
 '3\t4\t5\t3\t4\tzero both',
 '4\t4\t5\t2\t4\treversed width',
 '5\t4\t5\t3\t3\treversed height',
 '6\t4\t5\t3\t4\t'+'x'.repeat(4097),
 '7\t-1\t5\t0\t4\tnegative',
 '8\t4.5\t5\t4\t4\tfraction',
].join('\n');
process.stdout.write(JSON.stringify({default:api.review(text),optIn:api.review(text,null,{repairZeroArea:true}),
  stringOption:api.review(text,null,{repairZeroArea:'true'})}));
""")
    assert out["default"]["count"] == 0
    assert out["default"]["skippedCount"] == 8
    assert out["stringOption"] == out["default"]
    assert [issue["repairable"] for issue in out["default"]["issues"]] == [True] * 3 + [False] * 5
    assert out["optIn"]["repairedCount"] == 3
    assert out["optIn"]["skippedCount"] == 5
    assert [(r["x1"], r["y1"], r["x2"], r["y2"]) for r in out["optIn"]["rows"]] == [
        (4, 5, 4, 9), (4, 5, 8, 5), (4, 5, 4, 5),
    ]


def test_review_withholds_all_duplicate_indices_even_when_one_occurrence_is_malformed():
    out = run(r"""
const text='INDEX\tX1\tY1\tX2\tY2\tNOTE\n'+[
 '7\t4\t5\t6\t7\tvalid first',
 '007\t4\t5\t3\t4\tcollapsed duplicate',
 '8\tbad',
 '8\t4\t5\t6\t7\tvalid second',
 '9\tNaN\t5\t6\t7\tmalformed coordinate',
 '9\t4\t5\t6\t7\tvalid third',
 '10\t4\t5\t6\t7\tonly unique row',
].join('\n');
process.stdout.write(JSON.stringify(api.review(text,null,{repairZeroArea:true})));
""")
    assert out["count"] == 1 and out["rows"][0]["index"] == 10
    assert (out["skippedCount"], out["repairedCount"]) == (6, 0)
    assert [issue["index"] for issue in out["issues"]] == [7, 7, 8, 8, 9, 9]
    assert all(not issue["repairable"] and not issue["repaired"] for issue in out["issues"])
    assert all("all rows with this INDEX" in issue["message"] for issue in out["issues"])


def test_review_bounds_are_rechecked_against_new_reference_size_without_clipping():
    out = run(r"""
const text='INDEX\tX1\tY1\tX2\tY2\tNOTE\n'+[
 '1\t1\t1\t2\t2\tinside',
 '2\t90\t10\t110\t20\tpartly outside',
 '3\t100\t10\t99\t20\tcollapsed outside',
 '4\t99\t99\t98\t98\tlast pixel',
].join('\n');
process.stdout.write(JSON.stringify({small:api.review(text,{width:100,height:100},{repairZeroArea:true}),
  large:api.review(text,{width:200,height:200},{repairZeroArea:true})}));
""")
    small, large = out["small"], out["large"]
    assert [row["index"] for row in small["rows"]] == [1, 4]
    assert (small["repairedCount"], small["skippedCount"]) == (1, 2)
    assert not small["issues"][0]["repairable"] and not small["issues"][1]["repairable"]
    assert all("matching coordinate JPG" in issue["message"] for issue in small["issues"][:2])
    assert "has zero area (100, 10) → (99, 20)" in small["issues"][1]["message"]
    assert small["issues"][0]["original"]["x2"] == 110
    assert large["count"] == 4 and large["repairedCount"] == 2
    assert large["rows"][1]["x2"] == 110
    assert large["rows"][2]["x1"] == large["rows"][2]["x2"] == 100


def test_review_reports_zero_area_and_reference_mismatch_together():
    out = run(r"""
const text='INDEX\tX1\tY1\tX2\tY2\tNOTE\n199\t4877\t8248\t4876\t8247\t199';
process.stdout.write(JSON.stringify(api.review(text,{width:8000,height:5830},{repairZeroArea:true})));
""")
    assert out["count"] == 0 and out["skippedCount"] == 1 and out["repairedCount"] == 0
    issue = out["issues"][0]
    assert not issue["repairable"] and not issue["repaired"]
    assert "INDEX 199 has zero area (4877, 8248) → (4876, 8247)" in issue["message"]
    assert "outside the coordinate JPG 8000 × 5830" in issue["message"]
    assert "matching coordinate JPG" in issue["message"]


def test_review_reports_all_malformed_rows_and_preserves_physical_line_numbers():
    out = run(r"""
const text='\ufeff\r\nINDEX\tX1\tY1\tX2\tY2\tNOTE\r\n\r\n'+[
 'broken', '0\t1\t2\t3\t4\tbad index', '2\t1\t2\t3\t4\tA\textra',
 '3\t9007199254740993\t2\t3\t4\tunsafe integer',
].join('\r\n');
process.stdout.write(JSON.stringify(api.review(text)));
""")
    assert out["count"] == 0 and out["total"] == out["skippedCount"] == 4
    assert [issue["line"] for issue in out["issues"]] == [4, 5, 6, 7]
    assert [issue["index"] for issue in out["issues"]] == [None, None, 2, 3]
    assert all(issue["original"] is None for issue in out["issues"])


def test_review_file_errors_remain_fatal_and_invalid_rows_count_toward_limit():
    out = run(r"""
const header='INDEX\tX1\tY1\tX2\tY2\tNOTE\n', errors=[];
const limit=header+Array.from({length:2000},(_,i)=>`${i+1}\t4\t5\t3\t4\tempty`).join('\n');
for(const text of [null,'',header,'INDEX,X1,Y1,X2,Y2,NOTE\n1,1,1,1,1,note',
 header+'x'.repeat(9_000_000),limit+'\n2001\tbad']) {
 try {api.review(text);errors.push(null);}catch(e){errors.push(e.message);}
}
process.stdout.write(JSON.stringify({errors,total:api.review(limit).total,count:api.review(limit).count}));
""")
    assert all(isinstance(message, str) and message for message in out["errors"])
    assert "header" in out["errors"][3]
    assert "too large" in out["errors"][4]
    assert "2000" in out["errors"][5]
    assert out["total"] == 2000 and out["count"] == 0


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


def test_check_aspect_ratio_accepts_two_step_resizes_and_rejects_crops_and_small_distortion():
    out = run("""
const rows = [{ index: 1, x1: 10, y1: 10, x2: 20, y2: 20, note: 'ok' }];
const accepted = [
  [{ width: 8000, height: 5676 }, { width: 31578, height: 22409 }], // 3-1.nd2 2-step resize (1.117 px, 0.0197%)
  [{ width: 8000, height: 4679 }, { width: 38357, height: 22439 }], // A-1.nd2 2-step resize (1.032 px, 0.0221%)
  [{ width: 8000, height: 5471 }, { width: 32768, height: 22415 }], // 2-step resize
  [{ width: 10, height: 10 }, { width: 10, height: 11 }],            // 1 px base tolerance preserved on small images
].map(([grid, source]) => ({
  checked: api.checkAspectRatio(grid, source),
  mappedCount: api.mapToSource([{ index: 1, x1: 0, y1: 0, x2: 1, y2: 1, note: 'a' }], grid, source).length,
}));
const rejected = [
  [{ width: 8000, height: 5675 }, { width: 31578, height: 22409 }], // >2 px minor-axis drift (2.117 px)
  [{ width: 8000, height: 5674 }, { width: 31578, height: 22409 }], // >2 px minor-axis drift (3.117 px)
  [{ width: 10, height: 10 }, { width: 10, height: 12 }],            // 2 px on tiny image exceeds 0.5% relative cap
  [null, { width: 100, height: 100 }],
  [{ width: 0, height: 100 }, { width: 100, height: 100 }],
  [{ width: 100, height: 100 }, { width: 100.5, height: 100 }],
].map(([grid, source]) => {
  try { api.checkAspectRatio(grid, source); return null; }
  catch (e) { return e.message; }
});
let mapErr = null;
try { api.mapToSource(rows, { width: 8000, height: 5674 }, { width: 31578, height: 22409 }); }
catch (e) { mapErr = e.message; }
process.stdout.write(JSON.stringify({ accepted, rejected, mapErr }));
""")
    assert [entry["mappedCount"] for entry in out["accepted"]] == [1, 1, 1, 1]
    assert out["accepted"][0]["checked"] == {
        "grid": {"width": 8000, "height": 5676},
        "source": {"width": 31578, "height": 22409},
    }
    assert out["rejected"][0] == (
        "JPG/PNG (8000 × 5675) and current image (31578 × 22409) have different aspect ratios. "
        "Use the matching uncropped reference image."
    )
    assert out["rejected"][1] == (
        "JPG/PNG (8000 × 5674) and current image (31578 × 22409) have different aspect ratios. "
        "Use the matching uncropped reference image."
    )
    assert out["rejected"][2] == (
        "JPG/PNG (10 × 10) and current image (10 × 12) have different aspect ratios. "
        "Use the matching uncropped reference image."
    )
    assert out["mapErr"] == out["rejected"][1]
    assert all(isinstance(msg, str) and msg for msg in out["rejected"][3:])


def test_reference_header_pixel_product_cap_applies_to_jpeg_and_png():
    out = run(JPEG_SCRIPT + """
(async () => {
  const bigJpeg = blob([0xff, 0xd8, ...frame(0xc0, 50000, 40001), 0xff, 0xd9]);
  const okJpeg = blob([0xff, 0xd8, ...frame(0xc0, 50000, 40000), 0xff, 0xd9]);
  const u32 = (n) => [(n >>> 24) & 255, (n >>> 16) & 255, (n >>> 8) & 255, n & 255];
  const makePng = (w, h) => blob([
    137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13, 73, 72, 68, 82,
    ...u32(w), ...u32(h), 8, 2, 0, 0, 0, 0, 0, 0, 0,
  ]);
  const errors = [];
  for (const [fn, input] of [
    ['readJpegSize', bigJpeg],
    ['readImageSize', bigJpeg],
    ['readImageSize', makePng(50000, 40001)],
  ]) {
    try { await api[fn](input); errors.push(null); }
    catch (e) { errors.push(e.message); }
  }
  const ok = [await api.readJpegSize(okJpeg), await api.readImageSize(makePng(50000, 40000))];
  process.stdout.write(JSON.stringify({ maxPixels: api.MAX_REFERENCE_HEADER_PIXELS, errors, ok }));
})();
""")
    assert out["maxPixels"] == 2_000_000_000
    assert out["errors"] == ["The reference image dimensions exceed safety limits."] * 3
    assert out["ok"] == [{"width": 50000, "height": 40000}] * 2
