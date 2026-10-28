"""Region export buttons preserve the selected frame, LUT and pyramid scale."""

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from node_host import run_node

APP = Path(__file__).resolve().parents[1] / "nd2wsi/static/app.js"

SCRIPT = r"""
const fs=require('fs'), vm=require('vm');
const src=fs.readFileSync(process.argv[1], 'utf8'), cfg=JSON.parse(process.argv[2]);
const downloads=[], toasts=[], controls={};
const context={URLSearchParams, Number, Math,
  state:{roi:cfg.roi||{x:110,y:220,w:800,h:776}, channels:cfg.channels||[1],
    info:{channels:[0,1], levels:[{path:'3',downsample:8}], maxRenderMpx:400,generation:cfg.generation}},
  activeFrameContext:()=>cfg.hasFrame===false?null:{frame:{t:2,p:3,z:4}},
  frameOwnsRoi:()=>cfg.hasRoi!==false && cfg.hasFrame!==false,
  pixelSize:()=>cfg.pixelSize===undefined?[.8,.5]:cfg.pixelSize,
  roiScale:()=>cfg.noScale?null:({d:cfg.downsample||8,w:cfg.width||100,h:97}),
  levelPathFor:()=> cfg.levelPath||'3',
  lutParam:()=> '158:2144:1,204:2820:1.5',
  appendFrameParams:(q,f)=>{for(const [k,v] of Object.entries(f))q.set(k,v);},
  scopedButton:(id,disabled,title)=>{controls[id]={disabled,title};},
  showToast:t=>toasts.push(t),trackExport(){},
  document:{body:{append(){}},createElement:()=>({click(){downloads.push(this.href)},remove(){}})},
};
vm.createContext(context);
const start=src.indexOf('function scaleBarExportIssue('),end=src.indexOf('/* Poll the server',start);
vm.runInContext(src.slice(start,end),context);
// the real channel and render query helpers; lutParam above is the fixture's
const queryStart=src.indexOf('function channelQuery('),queryEnd=src.indexOf('function activeFrameParams(',queryStart);
vm.runInContext(src.slice(queryStart,queryEnd),context);
context.syncScaleBarExportControls();
for(const format of cfg.formats||['svg','jpg'])context.downloadRoi(format,['svg','jpg'].includes(format));
process.stdout.write(JSON.stringify({downloads,toasts,controls}));
"""


def run(config):
    result = run_node(SCRIPT, APP, json.dumps(config), check=True)
    return json.loads(result.stdout)


def test_both_buttons_use_actual_level_path_and_selected_frame_lut_channels():
    out = run({})
    assert len(out["downloads"]) == 2
    for fmt, url in zip(["svg", "jpg"], out["downloads"], strict=True):
        q = parse_qs(urlparse(url).query)
        assert q["format"] == [fmt] and q["scalebar"] == ["1"]
        assert q["level"] == ["3"]
        assert {k:q[k][0] for k in ["x","y","w","h"]} == dict(x="13",y="27",w="100",h="97")
        assert {k:q[k][0] for k in ["t","p","z"]} == dict(t="2",p="3",z="4")
        assert q["c"] == ["1"] and q["win"] == ["158:2144:1,204:2820:1.5"]
    assert all(not button["disabled"] for button in out["controls"].values())


def test_rendered_exports_name_committed_pyramid_and_raw_exports_stay_source_bound():
    out = run({"generation": "corrected-generation", "formats": ["svg", "jpg", "png", "tiff", "nd2"]})
    assert len(out["downloads"]) == 5
    for url in out["downloads"]:
        q = parse_qs(urlparse(url).query)
        assert "correction" not in q
        if q["format"][0] in {"svg", "jpg", "png"}:
            assert q["g"] == ["corrected-generation"]
            assert "win" in q
        else:
            assert "g" not in q and "win" not in q
            assert q["level"] == (["3"] if q["format"] == ["tiff"] else ["0"])


def test_whole_slide_tiff_honors_half_scale_without_applying_lut_or_changing_nd2():
    out = run({"formats": ["tiff", "nd2"], "downsample": 2, "levelPath": "1",
               "roi": {"x": 0, "y": 0, "w": 3406, "h": 3650}})
    tiff, nd2 = [parse_qs(urlparse(url).query) for url in out["downloads"]]
    assert {k: tiff[k][0] for k in ["level", "x", "y", "w", "h"]} == {
        "level": "1", "x": "0", "y": "0", "w": "1703", "h": "1825",
    }
    assert {k: nd2[k][0] for k in ["level", "w", "h"]} == {
        "level": "0", "w": "3406", "h": "3650",
    }
    for query in (tiff, nd2):
        assert query["c"] == ["1"] and "job" in query
        assert "win" not in query and "scalebar" not in query
        assert {k: query[k][0] for k in ["t", "p", "z"]} == {"t": "2", "p": "3", "z": "4"}


def test_tiff_can_still_stream_native_when_no_render_scale_fits():
    out = run({"formats": ["tiff"], "noScale": True})
    assert len(out["downloads"]) == 1
    query = parse_qs(urlparse(out["downloads"][0]).query)
    assert {k: query[k][0] for k in ["level", "w", "h"]} == {"level": "0", "w": "800", "h": "776"}
    assert out["toasts"] == []


@pytest.mark.parametrize("config,reason", [({"pixelSize":None},"calibration"),
                                         ({"pixelSize":[0,1]},"calibration"),
                                         ({"width":95},"larger export"),
                                         ({"hasRoi":False},"region"),
                                         ({"hasFrame":False},"site")])
def test_invalid_export_is_disabled_and_no_download_can_be_started(config, reason):
    out = run(config)
    assert out["downloads"] == []
    assert all(button["disabled"] and reason in button["title"] for button in out["controls"].values())


@pytest.mark.parametrize("fmt", ["nd2", "tiff", "png", "jpg", "svg"])
def test_every_format_refuses_when_no_channel_is_visible(fmt):
    out = run({"channels": [], "formats": [fmt]})
    assert out["downloads"] == []
    assert out["toasts"] == ["Turn on at least one channel to export"]


def test_all_visible_channels_omit_the_set_and_rendered_only_params_stay_off_raw_urls():
    out = run({"channels": [0, 1], "generation": "g7", "formats": ["nd2", "tiff", "png", "jpg"]})
    queries = [parse_qs(urlparse(url).query) for url in out["downloads"]]
    assert [q["format"][0] for q in queries] == ["nd2", "tiff", "png", "jpg"]
    for q in queries:
        assert "c" not in q
        rendered = q["format"][0] in {"png", "jpg"}
        assert ("win" in q) == rendered and ("g" in q) == rendered
