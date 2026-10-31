"""Exercise the production pane adapter and both renderers, not just the pure model."""
import re

import pytest
from test_astromap_ui_js import _run_export
from test_translucent_js import MODULE, run


def test_main_viewer_laser_style_does_not_change_user_annotations():
    result = _run_export(r'''
const f=fixture(),c=f.c,drawn=[];
c.state.viewer={};c.state.annotations=[];
for(const kind of ['box','geojson'])for(const laser of [true,false]){
  c.state.annotations.push({id:kind+laser,type:kind,x:10,y:20,w:3,h:4,text:'isolation #1',
    ...(laser?{slacs:{kind:'laser_shot'}}:{}),geojson:{geometry:{},properties:{}}});
}
c.window.Nd2AnnotationGeoJSON={imageParts:()=>({paths:[{d:'M 10 20 L 13 20 L 13 24 Z',closed:true}],points:[]}),
 label:()=> 'isolation #1',bounds:()=>({x:10,y:20})};
c.annColor=()=> '#123456';c.hexAlpha=(col,alpha)=>col+':'+alpha;
c.svgEl=(tag,attrs)=>{if(['polygon','path'].includes(tag))drawn.push(attrs);return {};};
c.chip=()=>({});c.renderLandmarks=()=>{};c.rememberAnnotationFrame=()=>{};c.toEl=(x,y)=>({x,y});
c.$('ann-layer').replaceChildren=()=>{};
vm.runInContext(production('renderAnnotations')+'\n'+production('renderImportedGeoJSON'),c);
c.renderAnnotations();return drawn;
''')
    assert [p["stroke"] for p in result] == ["#a0a6ae", "#123456", "#a0a6ae", "#123456"]
    assert [p["fill"] for p in result] == ["rgba(160,166,174,.16)", "#123456:0.08", "#a0a6ae:0.16", "#123456:0.08"]
    assert all(p["stroke-width"] == 1.5 and p["vector-effect"] == "non-scaling-stroke" for p in result)


def test_pane_adapter_adds_updates_and_acknowledges_refusals_and_exceptions():
    result = _run_export(r'''
const f=fixture(),c=f.c,replies=[];c.state.annotations=[];c.state.info={generation:'g',width:100,height:100};
c.currentSlideSid=()=> 'slide';c.window.Nd2SlacsLaser=require(path.join(path.dirname(process.argv[1]),'slacs-laser-v1.js'));
c.window.parent={pywebview:{api:{report_slacs_laser_box:r=>{replies.push(r);return Promise.resolve({ok:true});}}}};
let changed=0;c.addAnnotation=a=>{c.state.annotations.push(a);changed++;return a;};c.annotationsChanged=()=>changed++;
vm.runInContext(production('receiveSlacsLaserBox'),c);
const shot={sid:'slide',g:'g',p:null,token:'one',press:1,receiver_id:'receiver',at:'2026-01-01T00:00:00Z',
 target_px:[20,30],target_corners_px:[[15,25],[25,25],[25,35],[15,35]],pixel_size_um:[1,1],
 target_side_um:10,target_rotation_deg:0,target_calibrated:true};
c.receiveSlacsLaserBox(shot);c.receiveSlacsLaserBox({...shot,press:2,token:'two'});
c.locked=true;c.receiveSlacsLaserBox({...shot,press:3});c.locked=false;
c.window.Nd2SlacsLaser={receive(){throw Error('renderer unavailable');}};c.receiveSlacsLaserBox({...shot,press:4});
return {replies,items:c.state.annotations,meta:c.state.annLaserState,changed};
''')
    assert [r["decision"] for r in result["replies"]] == ["added", "updated", "skipped", "skipped"]
    assert [r["reason"] for r in result["replies"]][-2:] == ["annotations_locked", "pane_error"]
    assert all(r["sid"] == "slide" for r in result["replies"])
    assert len(result["items"]) == 1 and result["items"][0]["slacs"]["shots"] == 2
    assert result["meta"]["isolation_highwater"] == 1 and result["changed"] == 2


@pytest.mark.parametrize("angle,flip", [(0, False), (90, False), (90, True), (180, True), (270, False)])
def test_tracing_draws_only_confirmed_items_and_updates_one_id_in_image_coordinates(angle, flip):
    result = run(f'''
      const h=followHarness({{initialContext:{{rotation:{angle},flip:{str(flip).lower()}}}}}),layer=h.element('laser-annotations');
      const empty=layer.children.length,foreign=h.win.nd2TranslucentLaserItem({{id:'user',type:'box',x:1,y:2,w:3,h:4}});
      const item={{id:'laser',type:'box',x:410,y:260,w:10,h:20,text:'isolation #7',slacs:{{kind:'laser_shot',shots:2}}}};
      h.win.nd2TranslucentLaserItem(item);h.win.nd2TranslucentLaserItem({{...item,slacs:{{...item.slacs,shots:3}}}});
      const before=JSON.stringify(item),polygon=layer.children[0].attributes.points,label=layer.children[1].textContent;
      h.viewer.viewport.panTo(new Point(.45,.25));h.viewer.handlers['update-viewport']();
      return {{empty,foreign,count:layer.children.length,polygon,label,after:layer.children[0].attributes.points,unchanged:before===JSON.stringify(item)}};
    ''')
    import math
    angle_rad = math.radians(angle)
    actual = [[float(v) for v in point.split(",")] for point in result["polygon"].split()]
    for point, (x, y) in zip(actual, [(410, 260), (420, 260), (420, 280), (410, 280)], strict=True):
        dx, dy = (x - 400) * 2, (y - 250) * 2
        sx, sy = dx * math.cos(angle_rad) - dy * math.sin(angle_rad), dx * math.sin(angle_rad) + dy * math.cos(angle_rad)
        assert point == pytest.approx([400 + sx * (-1 if flip else 1), 250 + sy])
    assert result["empty"] == 0 and not result["foreign"] and result["count"] == 2 and result["unchanged"]
    assert result["label"] == "isolation #7 · 3 shots" and result["polygon"] != result["after"]
    css = (MODULE.parent / "translucent-v1.css").read_text()
    rule = re.search(r"#laser-annotations polygon\s*\{([^}]+)", css)[1]
    assert "rgba(160,166,174,.16)" in rule and "stroke: #a0a6ae" in rule and "stroke-width: 1.5" in rule


def test_delivery_stall_is_visible_and_clears_on_recovery():
    result = run('''
      const h=followHarness(),m=h.mounted;await m.pollSlacsDiscovery();await m.startStageFollow();await m.toggleLock();
      h.snapshot.laser_delivery='delivery_disabled';await m.pollStageFollow();
      const stalled=h.element('stage-follow-status').textContent;
      h.snapshot.laser_delivery='ready';await m.pollStageFollow();
      return {stalled,healthy:h.element('stage-follow-status').textContent,enabled:m.state.follow.enabled};
    ''')
    assert result["stalled"] == "Laser boxes paused: viewer busy"
    assert "paused" not in result["healthy"] and result["enabled"]


def test_singular_shot_labels_and_visible_flip_save_error():
    result = run('''
      const h=followHarness({bridgeOverrides:{async set_stage_follow_flips(){return {ok:true,saved:false,message:'Could not remember direction'};}}});
      await h.mounted.setFollowFlips({x:true,y:false});
      const item={id:'one',type:'box',x:10,y:10,w:2,h:2,text:'isolation #1',slacs:{kind:'laser_shot',shots:1,at:'2026-01-01T10:00:00Z'}};
      h.win.nd2TranslucentLaserItem(item);
      const pure=require(require('path').join(require('path').dirname(process.argv[1]),'slacs-laser-v1.js'));
      return {label:h.element('laser-annotations').children[1].textContent,pure:pure.label(item,true),list:pure.listLabel(item),
        hidden:h.element('stage-follow-flip-hint').hidden,error:h.element('stage-follow-flip-hint').textContent};
    ''')
    assert result["label"] == result["pure"] == "isolation #1 · 1 shot"
    assert " — 1 shot · " in result["list"] and "shots" not in result["list"]
    assert not result["hidden"] and result["error"] == "Could not remember direction"
