"""Only the pane may turn an observed physical press into a saved isolation."""
import json
from pathlib import Path

import pytest
from node_host import run_node

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi/static"
SETUP = r'''
const assert=require('assert'),api=require(process.argv[1]+'/slacs-laser-v1.js');
function harness() {
  const owner={sid:'slide',g:'image',p:null,ready:true,locked:false,loadFailed:false,quitPreparing:false,
    width:4000,height:4000,items:[],meta:null,
    add(item){this.items.push(item);return item;},
    update(old,item){this.items[this.items.indexOf(old)]=item;}};
  function shot(press,x=1000,y=1000,extra={}) {
    const radius=18/.66/Math.sqrt(2);
    return api.receive({sid:'slide',g:'image',p:null,press,token:'token',receiver_id:'receiver_with-hyphen',
      at:'2026-01-01T10:00:00Z',slacs_session:'instrument',target_px:[x,y],pixel_size_um:[.66,.66],
      target_side_um:18,target_rotation_deg:45,target_calibrated:true,
      target_corners_px:[[x-radius,y],[x,y-radius],[x+radius,y],[x,y+radius]],...extra},owner);
  }
  return {owner,shot};
}
'''


def run(body):
    result = run_node(SETUP + body, str(STATIC))
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout or "null")


def test_fifty_five_press_replay_makes_eight_numbered_bursts_and_retries_do_not_count():
    result = run('''
      const h=harness(),counts=[5,6,7,8,7,8,7,7],steps=[[0,0],[-15,-15],[-24,-30],[-30,-48],[24,54],[-124,232],[-139,247],[-154,265]];
      let press=0;
      counts.forEach((count,i)=>{for(let k=0;k<count;k++){
        const x=1000+steps[i][0],y=1000+steps[i][1];h.shot(++press,x,y);
        assert.equal(h.shot(press,x,y).reason,'duplicate_press');
      }});
      const items=JSON.parse(JSON.stringify(h.owner.items));
      h.owner.items=h.owner.items.filter(a=>a.slacs.isolation!==3);
      h.shot(++press,1300,1400,{target_calibrated:false});
      const ninth=h.owner.items.at(-1);
      // Empty sidecars still retain the monotonic number and idempotency history.
      const persisted=JSON.parse(JSON.stringify(h.owner.meta));h.owner.items=[];h.owner.meta=persisted;
      assert.equal(h.shot(press,1300,1400).reason,'duplicate_press');h.shot(++press,1600,1400);
      console.log(JSON.stringify({items,ninth,last:h.owner.items[0]}));
    ''')
    assert [item["slacs"]["shots"] for item in result["items"]] == [5, 6, 7, 8, 7, 8, 7, 7]
    assert [item["text"] for item in result["items"]] == [f"isolation #{i}" for i in range(1, 9)]
    assert result["ninth"]["text"] == "isolation #9_uncalibrated"
    assert result["last"]["text"] == "isolation #10"
    assert all(item["type"] == "geojson" and item["color"] == "#a0a6ae" and "astromap" not in item for item in result["items"])


@pytest.mark.parametrize("field,value,reason", [
    ("sid", "other", "wrong_slide"), ("ready", False, "owner_not_ready"),
    ("locked", True, "annotations_locked"), ("loadFailed", True, "load_failed"),
    ("quitPreparing", True, "quit_preparing"), ("g", "changed", "generation_changed"),
    ("p", 3, "site_changed"),
])
def test_every_owner_gate_runs_before_any_mutation(field, value, reason):
    result = run(f'''
      const h=harness();h.owner[{json.dumps(field)}]={json.dumps(value)};
      const r=h.shot(1);console.log(JSON.stringify({{r,items:h.owner.items,meta:h.owner.meta}}));
    ''')
    assert result["r"]["reason"] == reason
    assert result["items"] == [] and result["meta"] is None


def test_clipping_threshold_axis_aligned_shape_and_user_items_are_preserved():
    result = run('''
      const h=harness(),user={id:'manual',type:'box',x:995,y:995,w:10,h:10,text:'user'};
      h.owner.items.push(user);h.shot(1);assert.strictEqual(h.owner.items[0],user);
      const outside=h.shot(2,-1,100).reason;
      const clipped=h.shot(3,3995,3995,{target_corners_px:[[3990,3990],[4010,3990],[4010,4010],[3990,4010]]});
      const small=h.shot(4,3999,100,{target_corners_px:[[3999,90],[4039,90],[4039,110],[3999,110]]}).reason;
      console.log(JSON.stringify({outside,clipped,small,classification:clipped.item.geojson.properties.classification}));
    ''')
    assert result["outside"] == "outside_image" and result["small"] == "clipped_area"
    assert result["clipped"]["item"]["type"] == "box"
    assert result["clipped"]["box"] == {"x": 3990, "y": 3990, "w": 10, "h": 10}
    assert result["classification"] == {"name": "SLACS laser", "color": [160, 166, 174]}


def test_dedup_uses_stored_target_not_clipped_bounds_and_preserves_edited_labels():
    result = run('''
      const h=harness();h.shot(1);const a=h.owner.items[0];a.text='custom';
      a.geojson.geometry.coordinates=[[[0,0],[1,0],[1,1],[0,1],[0,0]]];
      const r=h.shot(2);console.log(JSON.stringify({r,items:h.owner.items,label:api.listLabel(r.item)}));
    ''')
    assert result["r"]["decision"] == "updated" and len(result["items"]) == 1
    assert result["r"]["item"]["text"] == "custom" and "2 shots" in result["label"]


def test_shell_routes_only_to_an_existing_pane_and_quit_freezes_delivery():
    run('''
      const fs=require('fs'),vm=require('vm'),source=fs.readFileSync(process.argv[1]+'/shell-v1.js','utf8');
      const code=source.slice(source.indexOf('window.nd2wsiSlacsLaserBox ='),source.indexOf('function finishQuitPreparation'));
      const sent=[],frame={contentWindow:{postMessage:(...v)=>sent.push(v)}};
      const host={window:{},frames:new Map([['slide',frame]]),location:{origin:'http://loopback'},quitPreparation:null,VIEWPORT_PROTOCOL_VERSION:1};
      vm.createContext(host);vm.runInContext(code,host);
      assert.equal(host.window.nd2wsiSlacsLaserBox({sid:'missing'}),false);
      assert.equal(host.window.nd2wsiSlacsLaserBox({sid:'slide',press:1}),true);
      host.quitPreparation={};assert.equal(host.window.nd2wsiSlacsLaserBox({sid:'slide'}),false);
      assert.equal(sent.length,1);assert.equal(sent[0][0].nd2wsi,'slacs-laser-box');
    ''')
