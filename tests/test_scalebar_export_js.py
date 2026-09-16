"""Region export buttons preserve the selected frame, LUT and pyramid scale."""

import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

APP = Path(__file__).resolve().parents[1] / "nd2wsi/static/app.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SCRIPT = r"""
const fs=require('fs'), vm=require('vm');
const src=fs.readFileSync(process.argv[1], 'utf8'), cfg=JSON.parse(process.argv[2]);
const downloads=[], toasts=[], controls={};
const context={URLSearchParams, Number, Math,
  state:{roi:{x:110,y:220,w:800,h:776}, channels:[1],
    info:{channels:[0,1], levels:[{path:'3',downsample:8}], maxRenderMpx:400}},
  activeFrameContext:()=>cfg.hasFrame===false?null:{frame:{t:2,p:3,z:4}},
  frameOwnsRoi:()=>cfg.hasRoi!==false && cfg.hasFrame!==false,
  pixelSize:()=>cfg.pixelSize===undefined?[.8,.5]:cfg.pixelSize,
  roiScale:()=>({d:8,w:cfg.width||100,h:97}),
  levelPathFor:()=> '3',
  lutParam:()=> '158:2144:1,204:2820:1.5',
  appendFrameParams:(q,f)=>{for(const [k,v] of Object.entries(f))q.set(k,v);},
  scopedButton:(id,disabled,title)=>{controls[id]={disabled,title};},
  showToast:t=>toasts.push(t),
  document:{body:{append(){}},createElement:()=>({click(){downloads.push(this.href)},remove(){}})},
};
vm.createContext(context);
const start=src.indexOf('function scaleBarExportIssue('),end=src.indexOf('let exportTimer',start);
vm.runInContext(src.slice(start,end),context);
context.syncScaleBarExportControls();
context.downloadRoi('svg',true);
context.downloadRoi('jpg',true);
process.stdout.write(JSON.stringify({downloads,toasts,controls}));
"""


def run(config):
    result = subprocess.run([NODE, "-e", SCRIPT, str(APP), json.dumps(config)],
                            check=True, capture_output=True, text=True, encoding="utf-8", timeout=20)
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


@pytest.mark.parametrize("config,reason", [({"pixelSize":None},"calibration"),
                                         ({"pixelSize":[0,1]},"calibration"),
                                         ({"width":95},"larger export"),
                                         ({"hasRoi":False},"region"),
                                         ({"hasFrame":False},"site")])
def test_invalid_export_is_disabled_and_no_download_can_be_started(config, reason):
    out = run(config)
    assert out["downloads"] == []
    assert all(button["disabled"] and reason in button["title"] for button in out["controls"].values())
