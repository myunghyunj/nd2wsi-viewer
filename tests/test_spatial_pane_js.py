"""Production pane lifetime guards with deliberately delayed open/message delivery."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi" / "static"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SCRIPT = r"""
const {PaneCommandGate, spatialContext} = require(process.argv[1] + '/spatial-pane-v1.js');
const gate = new PaneCommandGate('pane-1');
const frame = (p, t=0, z=0, generation='gen') =>
  spatialContext({sourceId:'source',generation,frame:{p,t,z}}, true);
gate.setContext(frame(1));
const lifecycle = (groupEpoch=1, enabled=true, groupSessionId='group-1') =>
  ({...gate.envelope(),groupEpoch,groupSessionId,enabled,spatialEnabled:true});
gate.bind(lifecycle());
const command = (seq=1, extra={}) =>
  ({...gate.envelope(),commandSeq:seq,nd2wsi:'viewport-apply',...extra});
process.stdout.write(JSON.stringify(eval(process.argv[2])));
"""


def run(body):
    result = subprocess.run(
        [NODE, "-e", SCRIPT, str(STATIC), body],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_delayed_open_after_stop_cannot_apply_old_viewport():
    assert run("""
      const pending = command();
      gate.receive(pending);
      const notReady = gate.takeReady(false);
      gate.bind(lifecycle(2, false));
      ({notReady, afterOpen:gate.takeReady(true), replay:gate.receive(pending)});
    """) == {"notReady": None, "afterOpen": None, "replay": False}


def test_latest_pending_slot_supersedes_all_earlier_commands_once():
    assert run("""
      gate.receive(command(1, {nd2wsi:'display-transform'}));
      gate.receive(command(3));
      const outOfOrder = gate.receive(command(2));
      const latest = gate.takeReady(true);
      ({outOfOrder, latest:latest.commandSeq, secondOpen:gate.takeReady(true),
        duplicate:gate.receive(command(3))});
    """) == {"outOfOrder": False, "latest": 3, "secondOpen": None, "duplicate": False}


def test_site_round_trip_never_revives_first_visit_callback():
    assert run("""
      const old = command();
      gate.receive(old);
      const epoch = gate.contextEpoch;
      gate.setContext(frame(2));
      const onP2 = gate.takeReady(true);
      gate.setContext(frame(1));
      ({onP2, sameOwner:gate.spatialContext.key===old.spatialContextKey,
        changedEpoch:gate.contextEpoch===epoch+2, oldRejected:!gate.receive(old)});
    """) == {"onP2": None, "sameOwner": True, "changedEpoch": True, "oldRejected": True}


def test_grid_context_refuses_every_spatial_command_but_keeps_group():
    assert run("""
      gate.setContext(null);
      gate.bind(lifecycle(2));
      const blocked = ['viewport-request','viewport-apply','viewport-nudge',
        'display-transform','landmark-mode'].every((nd2wsi,i)=>!gate.receive(command(i,{nd2wsi})));
      ({blocked, enabled:gate.enabled, session:gate.groupSessionId});
    """) == {"blocked": True, "enabled": True, "session": "group-1"}


def test_other_pane_grid_pause_clears_pending_even_when_local_site_is_ready():
    assert run("""
      gate.receive(command(9));
      gate.bind({...lifecycle(),spatialEnabled:false});
      const blocked=gate.receive(command(10));
      const afterOpen=gate.takeReady(true);
      gate.bind({...lifecycle(),spatialEnabled:true});
      ({blocked,afterOpen,oldDuplicate:gate.receive(command(9)),
        current:gate.receive(command(11))});
    """) == {"blocked": False, "afterOpen": None, "oldDuplicate": False, "current": True}


def test_edit_commands_work_when_unlinked_or_renderer_needs_relative_mode():
    assert run("""
      gate.bind({...lifecycle(),spatialEnabled:true,spatialReady:false,linked:false});
      gate.receive(command(1,{nd2wsi:'landmark-mode',editId:'edit',editRevision:0}));
      gate.takeReady(true).nd2wsi;
    """) == "landmark-mode"


def test_t_z_changes_share_spatial_owner_but_source_generation_does_not():
    assert run("""
      const epoch = gate.contextEpoch;
      const tzChanged = gate.setContext(frame(1,17,11));
      const sameEpoch = gate.contextEpoch === epoch;
      const sourceChanged = gate.setContext(frame(1,17,11,'new-generation'));
      ({tzChanged,sameEpoch,sourceChanged,epoch:gate.contextEpoch});
    """) == {"tzChanged": False, "sameEpoch": True, "sourceChanged": True, "epoch": 2}


def test_reload_rejects_old_instance_even_with_high_sequence():
    assert run("""
      const old = command(1000000);
      const reloaded = new PaneCommandGate('pane-2');
      reloaded.setContext(frame(1));
      reloaded.bind({...lifecycle(),...reloaded.envelope(),groupEpoch:1,groupSessionId:'group-1'});
      ({oldAccepted:reloaded.receive(old), freshAccepted:reloaded.receive({
        ...old,...reloaded.envelope(),commandSeq:1})});
    """) == {"oldAccepted": False, "freshAccepted": True}


def test_cancel_expiry_rejects_pending_draft_and_retired_compare_session():
    assert run("""
      const draft = command(8,{nd2wsi:'landmark-mode',editId:'old-edit',editRevision:3});
      gate.receive(draft);
      gate.bind(lifecycle(2)); // Cancel invalidates draft display work
      const oldDraft = gate.receive(draft);
      const afterOpen = gate.takeReady(true);
      const oldLife = lifecycle(3);
      gate.bind(lifecycle(1,true,'group-2'));
      const oldSession = gate.bind(oldLife);
      ({oldDraft, afterOpen, oldSession,
        fresh:gate.receive(command(1,{nd2wsi:'landmark-mode',editId:'new-edit',editRevision:0}))});
    """) == {"oldDraft": False, "afterOpen": None, "oldSession": False, "fresh": True}


@pytest.mark.parametrize("field,value", [
    ("paneInstanceId", "other"), ("contextEpoch", 999),
    ("spatialContextKey", "other"), ("groupSessionId", "other"), ("groupEpoch", 0),
])
def test_every_envelope_field_is_checked_at_receive_and_actual_apply(field, value):
    assert run(f"""
      const message = command();
      const stale = {{...message,{field}:{json.dumps(value)}}};
      const receive = gate.receive(stale);
      gate.receive(message);
      gate.pendingViewportCommand = stale; // stale callback must still be guarded
      ({{receive, apply:gate.takeReady(true)}});
    """) == {"receive": False, "apply": None}


def test_production_routes_share_one_guarded_slot_and_one_open_listener():
    app = (STATIC / "app.js").read_text()
    relay = app[app.index("function wireCompareRelay()") : app.index("/* ---- appearance")]
    assert relay.count('addHandler("open"') == 1
    assert 'addOnceHandler("open"' not in app[app.index("/* ---- linked compare") :]
    assert "flushPendingSpatialCommand();" in relay
    assert "receiveSpatialCommand(event.data);" in relay
    assert "receiveCompareLifecycle(event.data);" in relay
    assert "...landmarkEnvelope()" in relay
    assert "if (!spatialPaneReady()) return; // grid" in relay
    focus = app[app.index("function setPlateFocus(") : app.index("function setPlatePlaying(")]
    assert focus.index("refreshSpatialContext();") < focus.index("loadAnnotations(next)")
    assert "postSpatialReadiness();" in focus
    shortcut = app[app.index('if (plain && letterCode === "KeyL"') :]
    shortcut = shortcut[:shortcut.index('else if (ev.key === "Escape")')]
    assert "if (!spatialPaneReady()) return;" in shortcut
    assert "...spatialIdentity()" in shortcut


def test_landmarks_keep_point_identity_and_record_acquisition_provenance():
    app = (STATIC / "app.js").read_text()
    place = app[app.index("function placeLandmark(") : app.index("function undoLandmark(")]
    assert "id: crypto.randomUUID()" in place
    assert "...lm.points[nearest]" in place
    assert "const acquired = { t: frame?.t ?? null, z: frame?.z ?? null }" in place
    assert "lm.pointRevision += 1" in place


APP_SCRIPT = r"""
const fs = require('fs'), vm = require('vm');
const Nd2SpatialPane = require(process.argv[1] + '/spatial-pane-v1.js');
const source = fs.readFileSync(process.argv[1] + '/app.js','utf8');
const state = {
  info:{width:100,height:100,pixelSizeUm:[0.5,0.25]}, plate:{focus:1}, tool:null,
  viewportRelay:{seq:0,commandGate:new Nd2SpatialPane.PaneCommandGate('pane-1')},
  landmark:{active:false,points:[],needed:2,clickToZoom:null,pointRevision:0},
  viewer:{viewport:{},world:{getItemCount:()=>ready},gestureSettingsMouse:{clickToZoom:true}},
};
let ready = true, pointId = 0;
const outputs=[];
const parent={postMessage:message=>outputs.push(message)};
const context=vm.createContext({
  state, outputs, VIEWPORT_PROTOCOL_VERSION:2, VIEWPORT_EMIT_MS:50,
  window:{parent,Nd2SpatialPane}, location:{origin:'http://localhost'},
  crypto:{randomUUID:()=>`point-${++pointId}`},
  OpenSeadragon:{Point:class Point {constructor(x,y){this.x=x;this.y=y;}}},
  $:()=>({classList:{add(){},remove(){}}}),
  activeFrameContext:()=>({sourceId:'source',generation:'gen',frame:{p:state.plate.focus,t:17,z:3}}),
  currentSlideSid:()=> 'source', clearTimeout(){},setTimeout:fn=>{fn();return 1;},
  postSpatialReadiness:()=>outputs.push({ready:true}),
  applyDesiredDisplayTransform:()=>outputs.push({reset:true}),
  renderAnnotations(){},updateLandmarkHint(){},setTool(){},showToast(){},
  imgPoint:p=>p,toEl:(x,y)=>({x,y}),
  applyLinkedViewport:message=>outputs.push({applied:message.commandId}),
  applyDisplayTransform:message=>outputs.push({transformed:message.commandId}),
  nudgeView(){},postViewportState:(reason,extra)=>outputs.push({reason,...extra}),
  viewerElementToImagePoint:p=>({x:p.x*.5,y:p.y*.5}),
  setReady:value=>{ready=value},
  loadProduction:name=>vm.runInContext(production(name),context),
});
function production(name) {
  const start=source.indexOf('function '+name+'(');
  return source.slice(start,source.indexOf('\n}',start)+2);
}
for (const name of ['spatialIdentity','spatialImageReady','spatialPaneReady',
  'clearPaneSpatialWork','refreshSpatialContext','landmarkEnvelope','receiveCompareLifecycle',
  'receiveSpatialCommand','flushPendingSpatialCommand','setLandmarkMode','postLandmarkPoints',
  'placeLandmark','undoLandmark']) vm.runInContext(production(name),context);
vm.runInContext(`
  refreshSpatialContext();
  const gate=state.viewportRelay.commandGate;
  const life=(epoch=1,enabled=true)=>({...gate.envelope(),groupSessionId:'group',groupEpoch:epoch,enabled,spatialEnabled:true});
  receiveCompareLifecycle(life());
  const command=(seq,extra={})=>({...gate.envelope(),commandSeq:seq,...extra});
`,context);
process.stdout.write(JSON.stringify(vm.runInContext(process.argv[2],context)));
"""


def run_app(body):
    result = subprocess.run(
        [NODE, "-e", APP_SCRIPT, str(STATIC), body],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_production_open_flush_drops_cancelled_draft_before_new_edit():
    assert run_app("""
      setReady(false);
      const old=command(1,{nd2wsi:'landmark-mode',active:true,editId:'old',editRevision:0});
      receiveSpatialCommand(old);
      receiveCompareLifecycle(life(2));
      setReady(true);
      flushPendingSpatialCommand();
      const inactiveAfterOpen=!state.landmark.active;
      receiveSpatialCommand(command(1,{nd2wsi:'landmark-mode',active:true,editId:'new',editRevision:1}));
      receiveSpatialCommand(old);
      ({inactiveAfterOpen,editId:state.landmark.editId,active:state.landmark.active});
    """) == {"inactiveAfterOpen": True, "editId": "new", "active": True}


def test_production_point_move_keeps_id_and_reports_t_z_and_revision():
    result = run_app("""
      receiveSpatialCommand(command(1,{nd2wsi:'landmark-mode',active:true,editId:'edit',editRevision:7}));
      placeLandmark({x:1,y:1}); placeLandmark({x:80,y:80});
      placeLandmark({x:3,y:2}); // move point 1, preserve its structural ID
      const points=outputs.filter(m=>m.nd2wsi==='landmark-points');
      const last=points.at(-1);
      ({firstId:points[0].points[0].id,lastId:last.points[0].id,
        acquired:last.points[0].acquired,pointRevision:last.pointRevision,
        editId:last.editId,editRevision:last.editRevision,
        contextEpoch:last.contextEpoch,groupEpoch:last.groupEpoch});
    """)
    assert result == {
        "firstId": "point-1", "lastId": "point-1", "acquired": {"t": 17, "z": 3},
        "pointRevision": 3, "editId": "edit", "editRevision": 7,
        "contextEpoch": 1, "groupEpoch": 1,
    }


def test_unready_notice_preserves_calibration_and_advances_sequence():
    assert run_app("""
      setReady(false);
      loadProduction('viewportSnapshot'); loadProduction('postSpatialReadiness');
      postSpatialReadiness(); postSpatialReadiness();
      const ready=outputs.filter(m=>m.nd2wsi==='viewport-ready');
      ({seq:ready.map(m=>m.seq),ready:ready[1].imageReady,
        calibration:ready[1].pixelSizeUm,image:ready[1].imagePx,
        epoch:ready[1].contextEpoch,hasInventedCenter:'centerPx' in ready[1]});
    """) == {
        "seq": [1, 2], "ready": False, "calibration": {"x": .25, "y": .5},
        "image": {"x": 100, "y": 100}, "epoch": 1, "hasInventedCenter": False,
    }


PAN_VIEW = r"""
let center={x:100,y:200};
state.viewer.viewport={
  getContainerSize:()=>({x:800,y:600}),getCenter:()=>center,
  viewportToImageCoordinates:p=>p,imageToViewportCoordinates:p=>p,
  panTo:p=>{center={x:p.x,y:p.y};},
};
loadProduction('panByScreenDelta');
"""


def test_keyboard_nudge_reports_actual_source_delta_and_captured_revision():
    result = run_app(PAN_VIEW + """
      loadProduction('nudgeView');
      const key=command(1,{nd2wsi:'viewport-nudge',commandId:'key-1',committedRevision:7});
      gate.receive(key);nudgeView(1,-2,key);
      const reply=outputs.at(-1);
      ({center,delta:reply.nudgeDeltaPx,revision:reply.committedRevision,
        commandId:reply.nudgeCommandId,commandSeq:reply.commandSeq});
    """)
    assert result == {
        "center": {"x": 99.5, "y": 201}, "delta": {"x": -.5, "y": 1},
        "revision": 7, "commandId": "key-1", "commandSeq": 1,
    }


def test_option_drag_accumulates_only_real_drag_deltas_not_interleaved_zoom_center_changes():
    result = run_app(PAN_VIEW + """
      for(const name of ['beginAlignmentDrag','updateAlignmentDrag','finishAlignmentDrag'])loadProduction(name);
      receiveCompareLifecycle({...life(),linked:true,committedRevision:7});
      beginAlignmentDrag({button:0,altKey:true});
      const first={delta:{x:2,y:4}};updateAlignmentDrag(first);
      center={x:9876,y:5432}; // an unrelated zoom changed center between actual drag events
      const second={delta:{x:4,y:-2}};updateAlignmentDrag(second);
      finishAlignmentDrag();
      const reply=outputs.at(-1);
      ({firstPrevented:first.preventDefaultAction,secondPrevented:second.preventDefaultAction,
        delta:reply.nudgeDeltaPx,revision:reply.committedRevision,source:reply.nudgeSource});
    """)
    assert result == {
        "firstPrevented": True, "secondPrevented": True, "delta": {"x": -3, "y": -1},
        "revision": 7, "source": "drag",
    }


def test_holding_option_without_drag_does_not_turn_zoom_into_alignment_change():
    assert run_app("""
      loadProduction('scheduleViewportState');state.viewportRelay.altHeld=true;
      scheduleViewportState();const reply=outputs.at(-1);
      ({reason:reply.reason,nudge:Boolean(reply.nudge)});
    """) == {"reason": "user", "nudge": False}


def test_late_linked_viewport_cannot_interrupt_active_option_drag():
    assert run_app("""
      loadProduction('applyLinkedViewport');
      state.viewportRelay.alignmentDrag={dragId:'actual-drag'};
      const packet=command(1,{commandId:'late-pan'});gate.receive(packet);
      const before=outputs.length;applyLinkedViewport(packet);
      outputs.length===before;
    """) is True
