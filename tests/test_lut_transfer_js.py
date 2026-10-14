"""Conservative channel-name LUT interchange without changing source data."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "nd2wsi/static/lut-transfer-v1.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

HARNESS = r"""
const api=require(process.argv[1]);
const {currentLut,validateLut,fitsChannel,encode,planPaste}=api;
const channel=(label,start=0,end=255)=>({label,window:{start,end}});
const entry=(label,lo=10,hi=20,gamma=1)=>({label,lo,hi,gamma});
const payload=(channels,dtype='uint16')=>JSON.stringify({format:'nd2wsi-lut/1',dtype,channels});
const errorOf=fn=>{try{fn();return null;}catch(error){return error.message;}};
"""


def run(body):
    result = subprocess.run(
        [NODE, "-e", HARNESS + "const result=(()=>{" + body + "})();console.log(JSON.stringify(result));", str(MODULE)],
        capture_output=True, encoding="utf-8", check=True, timeout=20,
    )
    return json.loads(result.stdout)


def test_round_trip_matches_reordered_channels_and_preserves_default_and_explicit_values():
    out = run("""
const channels=[channel('CY5',129,672),channel('DAPI',0,0.75)];
const luts=[{lo:127,hi:4363,gamma:0.37},null];
const before=JSON.stringify({channels,luts});
const text=encode(channels,luts,'uint16');
const plan=planPaste(text,[channel('DAPI',0,0.75),channel('CY5')],'uint16');
plan.updates[0].lut.lo=999;
return {payload:JSON.parse(text),plan:planPaste(text,[channel('DAPI',0,0.75),channel('CY5')],'uint16'),
  preserved:before===JSON.stringify({channels,luts})};
""")
    assert out["preserved"] is True
    assert out["payload"] == {"format": "nd2wsi-lut/1", "dtype": "uint16", "channels": [
        {"label": "CY5", "lo": 127, "hi": 4363, "gamma": 0.37},
        {"label": "DAPI", "lo": 0, "hi": 0.75, "gamma": 1},
    ]}
    assert out["plan"] == {"updates": [
        {"index": 1, "lut": {"lo": 127, "hi": 4363, "gamma": 0.37}},
        {"index": 0, "lut": {"lo": 0, "hi": 0.75, "gamma": 1}},
    ], "skipped": 0}


def test_partial_and_disjoint_channels_have_no_index_fallback():
    out = run("""
const text=payload([entry('CY5'),entry('DAPI'),entry('FITC')]);
return {partial:planPaste(text,[channel('FITC'),channel('TRITC')],'uint16'),
  disjoint:planPaste(text,[channel('OTHER')],'uint16')};
""")
    assert out["partial"] == {"updates": [{"index": 0, "lut": {"lo": 10, "hi": 20, "gamma": 1}}], "skipped": 2}
    assert out["disjoint"] == {"updates": [], "skipped": 3}


def test_duplicate_source_and_target_names_skip_all_ambiguous_entries():
    out = run("""
const text=payload([entry('CY5'),entry(' CY5 '),entry('DAPI'),entry('FITC')]);
return planPaste(text,[channel('CY5'),channel('DAPI'),channel(' DAPI '),channel('FITC')],'uint16');
""")
    assert out == {"updates": [{"index": 3, "lut": {"lo": 10, "hi": 20, "gamma": 1}}], "skipped": 3}


def test_labels_are_trimmed_but_case_and_unicode_compatibility_are_not_conflated():
    out = run("""
return planPaste(payload([entry(' DAPI '),entry('cy5'),entry('ＦＩＴＣ'),entry('  '),entry(null),{}]),
  [channel('DAPI'),channel('CY5'),channel('FITC'),channel('')],'uint16');
""")
    assert out == {"updates": [{"index": 0, "lut": {"lo": 10, "hi": 20, "gamma": 1}}], "skipped": 5}


def test_invalid_values_and_types_are_skipped_without_coercion():
    out = run("""
return planPaste(payload([entry('ok'),entry('string','10'),entry('null',null),entry('reverse',20,10),
  entry('zero',10,10),entry('gammaLow',10,20,0.24),entry('gammaHigh',10,20,4.01),
  entry('bool',false),null,[],15]),
  ['ok','string','null','reverse','zero','gammaLow','gammaHigh','bool'].map(name=>channel(name)),'uint16');
""")
    assert out == {"updates": [{"index": 0, "lut": {"lo": 10, "hi": 20, "gamma": 1}}], "skipped": 10}


def test_invalid_duplicate_still_prevents_guessing_which_source_to_apply():
    out = run("""
return planPaste(payload([entry('DAPI'),{label:'DAPI',lo:'bad'},entry('CY5')]),
  [channel('DAPI'),channel('CY5')],'uint16');
""")
    assert out == {"updates": [{"index": 1, "lut": {"lo": 10, "hi": 20, "gamma": 1}}], "skipped": 2}


def test_validation_preserves_finite_fractional_ranges_and_gamma_boundaries():
    out = run("""
return {valid:[{lo:0,hi:0.75,gamma:0.25},{lo:-0.01,hi:-0.001,gamma:4},{lo:0,hi:1e-12,gamma:1}]
  .map(lut=>validateLut(lut,{dtype:'uint16'})),
  invalid:[null,[],{lo:NaN,hi:20,gamma:1},{lo:0,hi:Infinity,gamma:1},
    {lo:0,hi:20,gamma:NaN},{lo:-1e308,hi:1e308,gamma:1},{lo:0,hi:20,gamma:'1'}].map(lut=>validateLut(lut))};
""")
    assert out == {"valid": [True, True, True], "invalid": [False] * 7}


def test_default_and_explicit_current_luts_are_independent_copies():
    out = run("""
const ch=channel('DAPI',129,672),lut={lo:130,hi:500,gamma:0.37};
const defaultLut=currentLut(ch,null),explicitLut=currentLut(ch,lut);
defaultLut.lo=0;explicitLut.gamma=4;
return {ch,lut,defaultValue:currentLut(ch,undefined)};
""")
    assert out == {"ch": {"label": "DAPI", "window": {"start": 129, "end": 672}},
                   "lut": {"lo": 130, "hi": 500, "gamma": 0.37},
                   "defaultValue": {"lo": 129, "hi": 672, "gamma": 1}}


def test_dtype_conflicts_are_skipped_but_absent_dtype_is_compatible():
    out = run("""
return {conflict:planPaste(payload([entry('DAPI')]),[channel('DAPI')],'float32'),
  unknownSource:planPaste(payload([entry('DAPI')],null),[channel('DAPI')],'uint16'),
  unknownTarget:planPaste(payload([entry('DAPI')]),[channel('DAPI')]),
  whitespace:planPaste(payload([entry('DAPI')],' uint16 '),[channel('DAPI')],'uint16')};
""")
    assert out["conflict"] == {"updates": [], "skipped": 1}
    for key in ("unknownSource", "unknownTarget", "whitespace"):
        assert out[key] == {"updates": [{"index": 0, "lut": {"lo": 10, "hi": 20, "gamma": 1}}], "skipped": 0}


def test_integer_ranges_that_would_be_clamped_are_skipped_except_exact_target_default():
    out = run("""
const narrow={lo:0,hi:0.75,gamma:1};
return {normal:fitsChannel(channel('DAPI'),narrow,'uint16'),
  matching:fitsChannel(channel('DAPI',0,0.75),narrow,'uint16'),
  changedGamma:fitsChannel(channel('DAPI',0,0.75),{...narrow,gamma:0.5},'uint16'),
  float:fitsChannel(channel('DAPI'),narrow,'float32'),
  bool:fitsChannel(channel('DAPI'),narrow,'bool'),
  numpy:fitsChannel(channel('DAPI'),narrow,'<u2'),
  plan:planPaste(payload([entry('DAPI',0,0.75),entry('CY5',0,0.75)]),
    [channel('DAPI'),channel('CY5',0,0.75)],'uint16')};
""")
    assert out == {"normal": False, "matching": True, "changedGamma": False,
                   "float": True, "bool": False, "numpy": False,
                   "plan": {"updates": [{"index": 1, "lut": {"lo": 0, "hi": 0.75, "gamma": 1}}], "skipped": 1}}


def test_invalid_envelopes_and_unknown_versions_fail_with_readable_errors():
    out = run("""
const inputs=[null,'not json','null','[]','{}',JSON.stringify({format:'nd2wsi-lut/2',channels:[]}),
  JSON.stringify({format:'nd2wsi-lut/1',channels:{}}),payload([],123)];
return inputs.map(text=>errorOf(()=>planPaste(text,[],'uint16')));
""")
    assert all(isinstance(message, str) and message for message in out)
    assert "text" in out[0]
    assert "JSON" in out[1]
    assert "supported" in out[5]
    assert "pixel type" in out[7]


def test_payload_limits_count_utf8_bytes_and_channels():
    out = run("""
return {ascii:errorOf(()=>planPaste(' '.repeat(128*1024+1),[])),
  unicode:errorOf(()=>planPaste(payload([entry('가'.repeat(45000))]),[])),
  tooMany:errorOf(()=>planPaste(payload(Array.from({length:513},(_,i)=>entry(String(i)))),[])),
  limit:planPaste(payload(Array.from({length:512},(_,i)=>entry(String(i)))),[]),
  encodeOversize:errorOf(()=>encode([channel('가'.repeat(45000))],[null],'uint16'))};
""")
    assert "128 KiB" in out["ascii"] and "128 KiB" in out["unicode"]
    assert "512" in out["tooMany"]
    assert out["limit"] == {"updates": [], "skipped": 512}
    assert "128 KiB" in out["encodeOversize"]


def test_encode_refuses_invalid_values_instead_of_serializing_nan_to_null():
    out = run("""
return [errorOf(()=>encode([channel('DAPI')],[{lo:NaN,hi:20,gamma:1}],'uint16')),
  errorOf(()=>encode([channel('DAPI')],{},'uint16')),
  errorOf(()=>encode(Array.from({length:513},()=>channel('DAPI')),[],'uint16'))];
""")
    assert "invalid LUT" in out[0]
    assert "array" in out[1]
    assert "512" in out[2]
