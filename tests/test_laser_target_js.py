"""The target edits screen geometry; reports remain flip-aware image points."""
import pytest
from test_translucent_js import run


def test_target_zoom_rotation_flip_and_calibration_invalidation():
    result = run('''
      const h=followHarness({infoOverrides:{pixelSizeUm:[.5,.5]}}),m=h.mounted;
      Object.assign(h.win,{screenX:10,screenY:20,devicePixelRatio:2});
      await m.pollSlacsDiscovery();
      const reports=[];
      for(const [angle,flip] of [[0,false],[90,false],[90,true]]) {
        h.viewer.viewport.setRotation(angle);h.viewer.viewport.setFlip(flip);
        const r=m.target.report();reports.push(r);
      }
      const layer=h.element('laser-target');m.target.render();
      const p0=layer.children.find(p=>p.attributes.class==='laser-target-outline').attributes.points;
      h.viewer.viewport.zoomTo(h.viewer.viewport.getZoom()*2);m.target.render();
      const p1=layer.children.find(p=>p.attributes.class==='laser-target-outline').attributes.points;
      m.target.begin();const c=h.element('laser-target-capture');
      const event={button:0,pointerId:1,clientX:420,clientY:240,preventDefault(){},stopImmediatePropagation(){}};
      c.listeners.pointerdown(event);c.listeners.pointerup(event);
      const calibrated=m.target.report();h.win.screenX++;m.target.render();const moved=m.target.report();
      h.win.screenX--;h.win.devicePixelRatio=1;m.target.render();
      return {reports,p0,p1,calibrated,moved,dpr:m.target.report(),label:h.element('laser-target-status').textContent};
    ''')
    assert all(r["target_px"] == [400, 250] for r in result["reports"])
    assert result["p0"] != result["p1"]
    assert result["calibrated"]["target_calibrated"]
    assert result["calibrated"]["target_offset_css"] == [20, -10]
    assert not result["moved"]["target_calibrated"] and not result["dpr"]["target_calibrated"]
    assert "window moved or resized" in result["label"]


def test_calibration_captures_click_cancels_escape_before_close_and_ignores_drag():
    result = run('''
      const saved=[],h=followHarness({bridgeOverrides:{async save_laser_target(p){saved.push(p);return {ok:true,saved:true};}}}),m=h.mounted;
      Object.assign(h.win,{screenX:10,screenY:20,devicePixelRatio:2});await m.pollSlacsDiscovery();
      const before=JSON.stringify(api.camera(h.viewer));m.target.begin();let prevented=0,stopped=0;
      h.documentListeners.keydown({key:'Escape',target:{closest:()=>true},preventDefault(){prevented++;},stopImmediatePropagation(){stopped++;}});
      const cancelled=!m.target.calibrating && h.element('laser-target-capture').hidden;
      m.target.begin();const c=h.element('laser-target-capture'),e={button:0,pointerId:1,clientX:430,clientY:260,preventDefault(){},stopImmediatePropagation(){}};
      c.listeners.pointerdown(e);c.listeners.pointermove({...e,clientX:440});c.listeners.pointerup(e);
      const ignored=m.target.calibrating;
      c.listeners.pointerdown(e);c.listeners.pointerup({...e,clientX:431});
      for(let i=0;i<6;i++)await Promise.resolve();
      await m.toggleLock();const locked=m.target.begin();
      return {cancelled,prevented,stopped,ignored,locked,closes:h.nativeCalls.filter(x=>x[0]==='close').length,
        before,after:JSON.stringify(api.camera(h.viewer)),saved};
    ''')
    assert result["cancelled"] and result["ignored"]
    assert result["prevented"] == result["stopped"] == 1
    assert not result["locked"] and result["closes"] == 0 and result["before"] == result["after"]
    assert len(result["saved"]) == 1 and result["saved"][0]["calibrate"]
    assert result["saved"][0]["target"]["offset_css"] == [31, 10]


def test_resize_commits_once_and_inside_contextmenu_rotates_outside_chooses_rois():
    result = run('''
      const saved=[],h=followHarness({infoOverrides:{pixelSizeUm:[.5,.5]},bridgeOverrides:{async save_laser_target(p){saved.push(p);return {ok:true,saved:true};}}}),m=h.mounted;
      await m.pollSlacsDiscovery();const layer=h.element('laser-target');m.target.render();
      const handle=layer.children.find(p=>p.attributes.class==='laser-target-handle');
      const e={button:0,pointerId:3,target:handle,clientX:400,clientY:250,preventDefault(){},stopImmediatePropagation(){}};
      layer.listeners.pointerdown(e);layer.listeners.pointermove({...e,clientX:400,clientY:300});
      const during=saved.length;layer.listeners.pointerup(e);for(let i=0;i<6;i++)await Promise.resolve();
      const resized=m.target.target.side_um;
      h.element('translucent-stage').listeners.contextmenu(e);
      const plus=m.target.target.rotation_deg;
      h.element('translucent-stage').listeners.contextmenu({...e,shiftKey:true});
      const minus=m.target.target.rotation_deg;
      h.element('translucent-stage').listeners.contextmenu({...e,clientX:10,clientY:10});
      for(let i=0;i<12;i++)await Promise.resolve();
      await m.toggleLock();const locked=m.target.contextmenu(e);
      return {during,saved:saved.length,resized,plus,minus,locked,chooses:h.nativeCalls.filter(x=>x[0]==='choose').length};
    ''')
    assert result["during"] == 0 and result["saved"] == 3
    assert result["resized"] == pytest.approx(17.67766953)
    assert result["plus"] == 60 and result["minus"] == 45 and not result["locked"]
    assert result["chooses"] == 1


def test_context_bounds_and_page_hit_targets_leave_the_interior_and_centre_empty():
    result = run('''
      const t=require(require('path').join(require('path').dirname(process.argv[1]),'laser-target-v1.js'));
      return api.validateContext({...context,laser_target:{offset_css:[Infinity,0],side_um:500,rotation_deg:-15,window:[],dpr:-1}},context.slide_url).laser_target;
    ''')
    assert result == {"offset_css": [0, 0], "side_um": 200, "rotation_deg": 75, "window": None, "dpr": None}
    from test_translucent_js import MODULE
    css = (MODULE.parent / "translucent-v1.css").read_text()
    assert "stroke-width: 8; pointer-events: stroke" in css
    assert ".laser-reticle { stroke:" in css and "fill: none; pointer-events: none" in css
    page = (MODULE.parent / "translucent.html").read_text()
    for element in ("laser-target", "laser-target-capture", "laser-target-set", "laser-target-label", "laser-target-status"):
        assert f'id="{element}"' in page


def test_follow_start_and_discovery_loss_cancel_target_capture():
    result = run("""
      const h=followHarness(),m=h.mounted;await m.pollSlacsDiscovery();
      m.target.begin();await m.startStageFollow();const start=!m.target.calibrating;
      await m.stopStageFollow();m.target.begin();m.state.slacs.available=false;m.target.render();
      return {start,lost:!m.target.calibrating,hidden:h.element('laser-target-capture').hidden};
    """)
    assert result == {"start": True, "lost": True, "hidden": True}


@pytest.mark.parametrize("angle", range(0, 360, 15))
@pytest.mark.parametrize("flip", [False, True])
def test_nonzero_calibrated_target_and_all_corners_follow_rotation_and_flip(angle, flip):
    import math

    result = run(f'''
      const h=followHarness({{initialContext:{{rotation:{angle},flip:{str(flip).lower()},
        laser_target:{{offset_css:[27,-13],side_um:18,rotation_deg:45,window:[10,20,800,500],dpr:2}}}}}});
      Object.assign(h.win,{{screenX:10,screenY:20,devicePixelRatio:2}});await h.mounted.pollSlacsDiscovery();
      const target=require(require('path').join(require('path').dirname(process.argv[1]),'laser-target-v1.js'));
      const shapes=[];
      for(let rotation_deg=0;rotation_deg<90;rotation_deg+=15){{
        const b=target.shape(h.viewer.viewport,{{Point}},{{...h.mounted.target.target,rotation_deg}},{{x:.5,y:2}});
        shapes.push({{screen:b.corners,image:b.imageCorners}});
      }}
      return {{report:h.mounted.target.report(),shapes}};
    ''')
    radians = math.radians(angle)
    def image(point):
        dx = (point["x"] - 400) * (-1 if flip else 1)
        dy = point["y"] - 250
        return [400 + (dx * math.cos(radians) + dy * math.sin(radians)) / 2,
                250 + (-dx * math.sin(radians) + dy * math.cos(radians)) / 2]
    assert result["report"]["target_calibrated"]
    assert result["report"]["target_px"] == pytest.approx(image({"x": 427, "y": 237}))
    for shape in result["shapes"]:
        for screen, actual in zip(shape["screen"], shape["image"], strict=True):
            assert [actual["x"], actual["y"]] == pytest.approx(image(screen))
        for i, a in enumerate(shape["image"]):
            b = shape["image"][(i + 1) % 4]
            assert math.hypot((b["x"] - a["x"]) * .5, (b["y"] - a["y"]) * 2) == pytest.approx(18)
