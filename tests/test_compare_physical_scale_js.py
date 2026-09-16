"""Physical zoom is independent of image size, pane width and registration fit."""

import pytest
from test_compare_orientation_js import NODE, run

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SETUP = """
compare.members=['b'];compare.pairs.delete('c');
const a=compare.states.get('a'), b=compare.states.get('b');
const metric=st=>({x:st.spanPx.x/st.containerPx.x*st.pixelSizeUm.x,
  y:st.spanPx.x/st.containerPx.x*st.pixelSizeUm.y,cosine:0});
a.physicalScale=metric(a);b.physicalScale=metric(b);
"""


@pytest.mark.parametrize("mode", ["scale", "scale-frame"])
@pytest.mark.parametrize("pane_width", [400, 800, 1200])
def test_same_physical_zoom_across_calibration_and_different_pane_widths(mode, pane_width):
    out = run(SETUP + f"""
      compare.linkMode={mode!r};b.containerPx.x={pane_width};b.physicalScale=metric(b);
      messages.length=0;forwardViewport('a',a);
      const msg=messages.find(m=>m.nd2wsi==='viewport-apply'&&m.sid==='b');
      ({{umPerCss:msg.spanPx.x/b.containerPx.x*b.pixelSizeUm.x,
        center:msg.centerPx,scaleOnly:msg.scaleOnly,source:a.physicalScale.x}});
    """)
    assert out["umPerCss"] == pytest.approx(out["source"])
    assert out["scaleOnly"] is (mode == "scale")
    if mode == "scale":
        assert out["center"] == {"x": 620, "y": 390}


def test_scale_only_pan_does_not_emit_commands_or_modify_alignment():
    assert run(SETUP + """
      compare.linkMode='scale';
      b.physicalScale={...a.physicalScale};
      const before=JSON.stringify(compare.pairs.get('b'));
      a.centerPx={x:777,y:666};messages.length=0;forwardViewport('a',a);
      ({commands:messages.length,alignmentUnchanged:JSON.stringify(compare.pairs.get('b'))===before});
    """) == {"commands": 0, "alignmentUnchanged": True}


def test_fitted_scale_changes_center_mapping_but_not_physical_zoom():
    result = run(SETUP + """
      const pair=compare.pairs.get('b');pair.transform={a:2,b:0,c:0,d:2,tx:10,ty:20};
      messages.length=0;forwardViewport('a',a);
      const msg=messages.find(m=>m.nd2wsi==='viewport-apply'&&m.sid==='b');
      ({scale:msg.spanPx.x/b.containerPx.x*b.pixelSizeUm.x,center:msg.centerPx});
    """)
    assert result["scale"] == pytest.approx(.125)
    assert result["center"] == pytest.approx({"x": (321 * .25 * 2 + 10) / .66,
                                            "y": (211 * .25 * 2 + 20) / .66})


@pytest.mark.parametrize("broken", ["null", "{x:0,y:.5}", "{x:Infinity,y:.5}"])
def test_missing_invalid_calibration_never_silently_links_relative_scale(broken):
    result = run(SETUP + f"""
      b.pixelSizeUm={broken};messages.length=0;forwardViewport('a',a);
      ({{commands:messages.length,issue:scaleLinkIssue('scale')}});
    """)
    assert result["commands"] == 0
    assert "calibration" in result["issue"]


def test_incompatible_anisotropy_pauses_scale_link():
    result = run(SETUP + """
      b.physicalScale.y*=2;messages.length=0;forwardViewport('a',a);
      ({commands:messages.length,issue:scaleLinkIssue('scale')});
    """)
    assert result["commands"] == 0
    assert "Pixel shape" in result["issue"]


def test_shared_zoom_limit_constrains_the_driver_as_well_as_follower():
    result = run(SETUP + """
      compare.linkMode='scale';b.physicalScale.min=.2;
      messages.length=0;forwardViewport('a',a);
      messages.filter(m=>m.nd2wsi==='viewport-apply').map(m=>({sid:m.sid,
        um:m.spanPx.x/compare.states.get(m.sid).containerPx.x*compare.states.get(m.sid).pixelSizeUm.x,
        scaleOnly:m.scaleOnly}));
    """)
    assert [x["sid"] for x in result] == ["a", "b"]
    assert all(x["um"] == pytest.approx(.2) and x["scaleOnly"] for x in result)


def test_mode_transition_discards_late_pan_and_keeps_saved_alignment():
    assert run(SETUP + """
      const before=JSON.stringify(compare.pairs.get('b'));
      scheduleViewportRoute('a',a);
      const callbacks=getTimerCallbacks();
      setLinkMode('scale');replyAll();messages.length=0;
      callbacks.forEach(fn=>fn());
      ({mode:compare.linkMode,linked:compare.linked,
        oldCommands:messages.filter(m=>m.nd2wsi==='viewport-apply').length,
        alignmentUnchanged:JSON.stringify(compare.pairs.get('b'))===before});
    """) == {"mode": "scale", "linked": True, "oldCommands": 0, "alignmentUnchanged": True}


def test_scale_only_does_not_nudge_alignment_or_rotate_any_pane():
    assert run(SETUP + """
      compare.linkMode='scale';messages.length=0;
      nudgeAlignment(1,0,'b');applyDisplayTransforms();
      messages.length;
    """) == 0


def test_off_cancels_pending_mode_capture_without_relinking_on_late_replies():
    assert run(SETUP + """
      setLinkMode('scale-frame');const pending=compare.pendingRequest;
      setLinkMode('off');messages.length=0;replyAll(pending);
      ({linked:compare.linked,pending:!!compare.pendingRequest,
        commands:messages.filter(m=>m.nd2wsi==='viewport-apply').length});
    """) == {"linked": False, "pending": False, "commands": 0}


@pytest.mark.parametrize("mode", ["scale", "scale-frame"])
def test_menu_close_keeps_fresh_layout_sync_after_mode_capture(mode):
    result = run(SETUP + f"""
      compare.toolbarHeight=220;
      setLinkMode({mode!r});replyAll();
      const hasLayoutTimer=!!compare.layoutRequestTimer;
      const resized={{...compare.states.get('a'),spanPx:{{x:460,y:300}}}};
      resized.physicalScale=metric(resized);messages.length=0;
      runTimers(80);const kind=compare.pendingRequest?.kind;
      replyAll(compare.pendingRequest,{{a:resized}});
      const msg=messages.find(m=>m.nd2wsi==='viewport-apply'&&m.sid==='b');
      ({{hasLayoutTimer,kind,um:msg.spanPx.x/b.containerPx.x*b.pixelSizeUm.x,
        expected:resized.physicalScale.x}});
    """)
    assert result["hasLayoutTimer"] and result["kind"] == "sync"
    assert result["um"] == pytest.approx(result["expected"])
