"""Production keyboard routing before the bundled OpenSeadragon target listener.

The VM models DOM capture/target/bubble delivery, including cancellation. The
OSD keyboard dispatcher and viewer action handler come from the bundled vendor
file; app classification, ownership, orientation and pane identity guards also
execute their production implementations.
"""

import json
import subprocess

import pytest
from test_compare_orientation_js import NODE, STATIC
from test_compare_orientation_js import run as run_shell

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SCRIPT = r"""
const fs=require('fs'), vm=require('vm');
const root=process.argv[1], config=JSON.parse(process.argv[2]);
// Never let the machine running Node select the scenario's keyboard layout.
Object.defineProperty(globalThis,'navigator',{
  value:{platform:config.platform || 'MacIntel'},configurable:true,
});
const source=fs.readFileSync(root+'/app.js','utf8');
const vendor=fs.readFileSync(root+'/vendor/openseadragon/openseadragon.min.js','utf8');
const Router=require(root+'/shortcut-router-v1.js');
const {PaneCommandGate}=require(root+'/spatial-pane-v1.js');
function production(name) {
  const start=source.indexOf('function '+name+'(');
  if(start<0) throw Error('Missing production function: '+name);
  return source.slice(start,source.indexOf('\n}',start)+2);
}
// Extract by the public event name, not a copied reimplementation of OSD keys.
const actionAt=vendor.indexOf('this.raiseEvent("canvas-key",');
const actionStart=vendor.lastIndexOf('function ',actionAt);
const actionEnd=vendor.indexOf('function ',actionAt);
const dispatchAt=vendor.indexOf('keydown:function(e)');
const dispatchEnd=vendor.indexOf(',keyup:function',dispatchAt);
if(actionAt<0 || actionStart<0 || actionEnd<0 || dispatchAt<0 || dispatchEnd<0) {
  throw Error('Bundled OSD keyboard structure changed; inspect the real handlers');
}
const out={clicks:[],tools:[],messages:[],toasts:[],applied:[],orientationCalls:[],
  osdRotations:[],osdFlips:0,pans:[],zooms:[],homes:0,autofocus:[],
  targetDeliveries:0,osdKeydowns:0,events:[]};
const listeners=new Map(), elements=new Map(), viewerHandlers=new Map();
function element(id='') {
  if(!elements.has(id)) elements.set(id,{
    id,tagName:'DIV',disabled:!!config.disabled?.includes(id),hidden:true,
    closest:()=>null,addEventListener(){},classList:{contains:()=>false},
    click(){out.clicks.push(id);},
  });
  return elements.get(id);
}
const gate=new PaneCommandGate('pane-test');
gate.setContext(config.grid ? null : {key:'context-test',kind:'slide'});
if(config.compare) gate.bind({...gate.envelope(),groupSessionId:'group-test',
  groupEpoch:1,enabled:true,spatialEnabled:config.ready!==false});
const state={
  quitPreparing:false,roi:null,tool:null,pixel:{cursor:null},tabCount:2,
  landmark:{active:!!config.landmark,points:[]},
  info:{plate:{Z:config.zCount ?? 5,T:4}},
  plate:config.plate ? {auto:false,focus:config.grid ? null : 0,t:0,placed:[]} : null,
  viewportRelay:{commandGate:gate,compare:config.compare || null,
    displayRotation:config.degrees || 0,displayFlipped:!!config.flipped,
    reopening:false,contextChanging:false},
};
let rotation=config.degrees || 0, flipped=!!config.flipped;
const viewport={
  getRotation:()=>rotation,getFlip:()=>flipped,
  get flipped(){return flipped;},
  setRotation(value){rotation=value;out.osdRotations.push(value);},
  toggleFlip(){flipped=!flipped;out.osdFlips++;},
  applyConstraints(){},deltaPointsFromPixels:p=>p,
  panBy:p=>out.pans.push(p),zoomBy:value=>out.zooms.push(value),goHome:()=>out.homes++,
};
const viewer={viewport,world:{getItemCount:()=>config.ready===false ? 0 : 1},
  addHandler(name,callback){
    if(!viewerHandlers.has(name)) viewerHandlers.set(name,[]);
    viewerHandlers.get(name).push(callback);
  },
  raiseEvent(name,event){for(const callback of viewerHandlers.get(name) || []) callback(event);},
  rotationIncrement:90,pixelsPerArrowPress:40,
  panHorizontal:true,panVertical:true,goToPreviousPage(){},goToNextPage(){}};
state.viewer=viewer;
const window={Nd2ShortcutRouter:Router,addEventListener(kind,fn,options){
  if(!listeners.has(kind)) listeners.set(kind,[]);
  listeners.get(kind).push({fn,capture:options===true || !!options?.capture});
}};
window.parent=config.iframe ? {postMessage:data=>out.messages.push(data)} : window;
const context=vm.createContext({
  state,window,document:{body:element('body')},location:{origin:'http://qa.invalid'},
  VIEWPORT_PROTOCOL_VERSION:2,$:element,structuredClone,
  OpenSeadragon:()=>viewer,makeTileSource:()=>({}),
  renderAnnotations(){},updateAlignmentDrag(){},debounceToast:()=>()=>{},
  refreshSpatialContext(){},postSpatialReadiness(){},currentSlideSid:()=> 'test',
  showToast:message=>out.toasts.push(message),
  setTool:tool=>{state.tool=tool;out.tools.push(tool);},togglePixelInspector(){},
  setPlateAuto:value=>{state.plate.auto=value;out.autofocus.push(value);},
  stepPlateZ(){},setPlateT(){},setPlatePlaying(){},setPlateFocus(){},
  applyDesiredDisplayTransform(){
    rotation=state.viewportRelay.displayRotation;flipped=state.viewportRelay.displayFlipped;
    out.applied.push({degrees:rotation,flipped});return true;
  },
});
for(const name of ['buildViewer','normalizedRotation','spatialIdentity','spatialImageReady',
  'spatialPaneReady','applyOrientationShortcut','wirePlateKeys','wireCompareRelay','wireKeys']) {
  vm.runInContext(production(name),context);
}
const applyOrientation=context.applyOrientationShortcut;
context.applyOrientationShortcut=action=>{
  out.orientationCalls.push(action);return applyOrientation(action);
};
// Keep real viewer keyboard-event hooks and initialization order too.
context.buildViewer();
if(config.plate) context.wirePlateKeys();
context.wireCompareRelay();
context.wireKeys();
const action=vm.runInNewContext('('+vendor.slice(actionStart,actionEnd)+')',{
  m:{Point:function(x,y){this.x=x;this.y=y;}},
});
const tracker={keyDownHandler:event=>{out.osdKeydowns++;action.call(viewer,event);}};
const osdKeydown=vm.runInNewContext('('+vendor.slice(dispatchAt+8,dispatchEnd)+')',{
  i:tracker,
  // MouseTracker preprocessing defaults for a keyboard event, without a
  // custom preProcessEventHandler; vendor dispatch still owns cancellation.
  H:(tracker,event)=>Object.assign(event,{preventGesture:false,
    defaultPrevented:!!event.originalEvent.defaultPrevented,preventDefault:false,
    stopPropagation:false}),
  c:{cancelEvent:event=>event.preventDefault(),stopEvent:event=>event.stopPropagation()},
});
function press(spec) {
  const targetSpec=spec.target || {};
  const target={tagName:targetSpec.tag || 'DIV',
    isContentEditable:!!targetSpec.editable,
    closest(selector){
      if(targetSpec.insideEditable && selector.includes('contenteditable')) return {};
      if(targetSpec.insideTextbox && selector.includes('[role="textbox"]')) return {};
      if(targetSpec.plateMenu && selector.includes('#plate-view-menu')) return {};
      return null;
    }};
  const event={key:'',code:'',keyCode:0,repeat:false,metaKey:false,ctrlKey:false,
    altKey:false,shiftKey:false,defaultPrevented:false,stopped:false,immediate:false,
    ...spec,target,
    preventDefault(){this.defaultPrevented=true;},
    stopPropagation(){this.stopped=true;},
    stopImmediatePropagation(){this.stopped=true;this.immediate=true;},
  };
  const handlers=listeners.get('keydown') || [];
  for(const item of handlers.filter(item=>item.capture)) {
    item.fn(event);if(event.immediate) break;
  }
  // Text fields are outside the OSD canvas. IME events on the canvas can
  // still reach it, carrying their real 229 keyCode instead of Latin R/F.
  if(!event.stopped && !spec.outsideCanvas) {
    out.targetDeliveries++;osdKeydown(event);
  }
  if(!event.stopped) for(const item of handlers.filter(item=>!item.capture)) {
    item.fn(event);if(event.immediate) break;
  }
  out.events.push({prevented:event.defaultPrevented,stopped:event.stopped});
}
for(const event of config.events || []) press(event);
if(config.message) {
  const data={nd2wsi:'pane-orientation-shortcut',version:2,action:'rotate-right',
    ...gate.envelope(),...config.message};
  if(config.changeContext) gate.setContext({key:'new-context',kind:'slide'});
  for(const item of listeners.get('message') || []) item.fn({
    origin:'http://qa.invalid',source:window.parent,data,
  });
}
out.pose={degrees:state.viewportRelay.displayRotation,flipped:state.viewportRelay.displayFlipped};
process.stdout.write(JSON.stringify(out));
"""


def pane(events=(), **config):
    result = subprocess.run(
        [NODE, "-e", SCRIPT, str(STATIC), json.dumps({"events": events, **config})],
        capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def key(letter, **kwargs):
    return {"key": letter.lower(), "code": "Key" + letter.upper(),
            "keyCode": ord(letter.upper()), **kwargs}


@pytest.mark.parametrize("letter,button,tool", [
    ("R", "tb-region", None), ("A", "tb-annot", None),
    ("C", "tb-channels", None), ("P", None, "pin"),
])
def test_owned_letters_run_before_osd_canvas_defaults(letter, button, tool):
    out = pane([key(letter)])
    assert out["clicks"] == ([button] if button else [])
    assert out["tools"] == ([tool] if tool else [])
    assert out["targetDeliveries"] == out["osdKeydowns"] == 0
    assert out["osdRotations"] == out["pans"] == []
    assert out["osdFlips"] == 0
    assert out["events"] == [{"prevented": True, "stopped": True}]


@pytest.mark.parametrize("config,autofocus", [
    ({}, []), ({"plate": True}, [True]),
    ({"plate": True, "zCount": 1}, []),
    ({"plate": True, "disabled": ["t-auto"]}, []),
    ({"plate": True, "compare": {"enabled": True, "linked": True}}, []),
    ({"plate": True, "landmark": True}, []),
])
def test_plain_f_only_autofocuses_an_eligible_plate_and_never_flips(config, autofocus):
    out = pane([key("F")], **config)
    assert out["autofocus"] == autofocus
    assert out["osdFlips"] == out["osdKeydowns"] == 0
    assert out["orientationCalls"] == []
    assert out["pose"] == {"degrees": 0, "flipped": False}


@pytest.mark.parametrize("letter", ["f", "ㄹ"])
def test_holding_english_or_korean_f_toggles_plate_autofocus_only_once(letter):
    out = pane([key("F", key=letter), key("F", key=letter, repeat=True),
                key("F", key=letter, repeat=True)], plate=True)
    assert out["autofocus"] == [True]
    assert out["events"] == [{"prevented": True, "stopped": True}] * 3
    assert out["orientationCalls"] == []
    assert out["targetDeliveries"] == out["osdKeydowns"] == out["osdFlips"] == 0
    assert out["pose"] == {"degrees": 0, "flipped": False}


@pytest.mark.parametrize("letter", ["F", "ㄹ"])
def test_shift_f_is_reserved_without_autofocus_or_osd_flip_on_a_plate(letter):
    out = pane([key("F", key=letter, shiftKey=True),
                key("F", key=letter, shiftKey=True, repeat=True)], plate=True)
    assert out["autofocus"] == out["orientationCalls"] == []
    assert out["events"] == [{"prevented": True, "stopped": True}] * 2
    assert out["targetDeliveries"] == out["osdKeydowns"] == out["osdFlips"] == 0


def test_korean_ime_composition_on_a_plate_does_not_autofocus_or_flip():
    out = pane([key("F", key="ㄹ", keyCode=229, isComposing=True)], plate=True)
    assert out["events"] == [{"prevented": False, "stopped": False}]
    assert out["autofocus"] == out["orientationCalls"] == []
    assert out["osdFlips"] == 0
    assert out["pose"] == {"degrees": 0, "flipped": False}


@pytest.mark.parametrize("letter,composed", [("R", "ㄱ"), ("F", "ㄹ")])
@pytest.mark.parametrize("plate", [False, True])
def test_osd_vetoes_composition_even_when_the_browser_reports_a_latin_keycode(
    letter, composed, plate,
):
    out = pane([key(letter, key=composed, isComposing=True)], plate=plate)
    # Input remains available to the browser; only OSD's spatial action is
    # vetoed by the real canvas-key listener registered in buildViewer.
    assert out["events"] == [{"prevented": False, "stopped": False}]
    assert out["targetDeliveries"] == out["osdKeydowns"] == 1
    assert out["autofocus"] == out["orientationCalls"] == out["osdRotations"] == []
    assert out["osdFlips"] == 0
    assert out["pose"] == {"degrees": 0, "flipped": False}


@pytest.mark.parametrize("letter,action,pose", [
    ("R", "rotate-right", {"degrees": 90, "flipped": False}),
    ("F", "flip-horizontal", {"degrees": 0, "flipped": True}),
])
@pytest.mark.parametrize("platform,modifier", [("MacIntel", "metaKey"), ("Win32", "ctrlKey")])
def test_command_orientation_runs_the_production_action_once(letter, action, pose, platform, modifier):
    out = pane([key(letter, **{modifier: True})], platform=platform)
    assert out["orientationCalls"] == [action]
    assert out["applied"] == [pose]
    assert out["pose"] == pose
    assert out["clicks"] == out["tools"] == []
    assert out["osdKeydowns"] == out["osdFlips"] == 0
    assert out["events"] == [{"prevented": True, "stopped": True}]


def test_clockwise_rotation_reverses_the_osd_increment_when_mirrored():
    out = pane([key("R", metaKey=True)], degrees=0, flipped=True)
    assert out["pose"] == {"degrees": 270, "flipped": True}
    assert out["applied"] == [out["pose"]]


@pytest.mark.parametrize("letter,meta", [("R", False), ("F", False), ("R", True), ("F", True)])
def test_held_keys_are_reserved_without_repeating_panels_or_orientation(letter, meta):
    out = pane([key(letter, metaKey=meta, repeat=True)])
    assert out["events"] == [{"prevented": True, "stopped": True}]
    assert out["clicks"] == out["orientationCalls"] == out["applied"] == []
    assert out["osdKeydowns"] == 0


@pytest.mark.parametrize("target", [
    {"tag": "INPUT"}, {"tag": "TEXTAREA"}, {"tag": "SELECT"},
    {"editable": True}, {"insideEditable": True}, {"insideTextbox": True},
])
def test_text_editing_keeps_plain_and_command_keys(target):
    out = pane([key("R", target=target, outsideCanvas=True),
                key("F", target=target, outsideCanvas=True, metaKey=True)])
    assert out["events"] == [{"prevented": False, "stopped": False}] * 2
    assert out["clicks"] == out["orientationCalls"] == out["applied"] == []


def test_ime_composition_remains_unhandled_and_latin_shortcuts_work_afterward():
    out = pane([key("R", key="ㄱ", keyCode=229, isComposing=True),
                key("R", key="ㄱ", keyCode=82)])
    assert out["events"] == [{"prevented": False, "stopped": False},
                             {"prevented": True, "stopped": True}]
    assert out["clicks"] == ["tb-region"]
    assert out["osdRotations"] == []


@pytest.mark.parametrize("event,expected", [
    ({"key": "ArrowLeft", "code": "ArrowLeft", "keyCode": 37}, {"pans": [{"x": -40, "y": 0}]}),
    ({"key": "+", "code": "Equal", "keyCode": 187, "shiftKey": True}, {"zooms": [1.1]}),
    ({"key": "-", "code": "Minus", "keyCode": 189}, {"zooms": [.9]}),
])
def test_unowned_navigation_still_reaches_the_bundled_osd_handler(event, expected):
    out = pane([event])
    assert out["targetDeliveries"] == out["osdKeydowns"] == 1
    for name, value in expected.items():
        assert out[name] == value
    assert out["events"] == [{"prevented": True, "stopped": False}]


@pytest.mark.parametrize("config", [{"ready": False}, {"plate": True, "grid": True}])
def test_orientation_cannot_change_an_unready_or_grid_pane(config):
    out = pane([key("R", metaKey=True)], **config)
    assert out["applied"] == []
    assert out["toasts"]
    assert out["pose"] == {"degrees": 0, "flipped": False}


def test_linked_pane_relays_orientation_with_current_identity_instead_of_applying_locally():
    out = pane([key("R", metaKey=True)], iframe=True,
               compare={"enabled": True, "linked": True})
    assert out["applied"] == []
    assert len(out["messages"]) == 1
    assert out["messages"][0] == {
        "nd2wsi": "compare-orientation-shortcut", "version": 2, "sid": "test",
        "action": "rotate-right", "groupSessionId": "group-test", "groupEpoch": 1,
        "paneInstanceId": "pane-test", "contextEpoch": 1,
        "spatialContextKey": "context-test",
        "spatialContext": {"key": "context-test", "kind": "slide"},
    }


@pytest.mark.parametrize("config,accepted", [
    ({}, True), ({"changeContext": True}, False),
    ({"message": {"paneInstanceId": "old-pane"}}, False),
    ({"message": {"contextEpoch": 0}}, False),
    ({"message": {"spatialContextKey": "other-site"}}, False),
    ({"compare": {"enabled": True, "linked": True}}, False),
])
def test_shell_focused_message_is_bound_to_the_same_local_pane_context(config, accepted):
    out = pane(iframe=True, **{"message": {}, **config})
    assert len(out["applied"]) == int(accepted)
    assert len(out["orientationCalls"]) == int(accepted)


@pytest.mark.parametrize("letter,pose", [
    ("r", {"degrees": 90, "flipped": False}),
    ("f", {"degrees": 0, "flipped": True}),
])
@pytest.mark.parametrize("platform,modifier", [("MacIntel", "metaKey"), ("Win32", "ctrlKey")])
def test_shell_command_orientation_targets_active_linked_slide(letter, pose, platform, modifier):
    out = run_shell(f"""
      active='a'; compare.orientationSid='c';
      const consumed=pressKey('{letter}',{{code:'Key{letter.upper()}',{modifier}:true}});
      const target=compare.pendingRequest?.targetSid;
      replyAll();
      ({{consumed,target,pose:displayTransformFor('c'),other:displayTransformFor('b')}});
    """, platform=platform)
    assert out == {"consumed": {"prevented": True, "stopped": True}, "target": "c",
                   "pose": pose, "other": {"degrees": 0, "flipped": False}}


@pytest.mark.parametrize("overrides", [
    "{ctrlKey:true}", "{metaKey:true,ctrlKey:true}",
    "{metaKey:true,altKey:true}", "{metaKey:true,shiftKey:true}",
    "{metaKey:true,repeat:true}",
    "{metaKey:true,target:{tagName:'INPUT',closest:()=>null}}",
    "{metaKey:true,isComposing:true}",
])
def test_shell_orientation_uses_strict_command_modifiers_and_typing_guards(overrides):
    out = run_shell(f"""
      const consumed=pressKey('r',{{code:'KeyR',...{overrides}}});
      ({{consumed,pending:compare.pendingRequest,commands:messages.filter(m=>m.nd2wsi)}});
    """)
    assert out["pending"] is None
    assert out["commands"] == []
    assert out["consumed"]["prevented"] is ("repeat:true" in overrides)


def test_shell_shortcut_preserves_a_committed_fit():
    out = run_shell("""
      fitAll(); messages.length=0;
      const before=JSON.stringify(compare.pairs.get('b'));
      pressKey('r',{code:'KeyR',metaKey:true});
      ({unchanged:before===JSON.stringify(compare.pairs.get('b')),
        pending:compare.pendingRequest,errors:messages.filter(m=>m.error).map(m=>m.error),
        commands:messages.filter(m=>m.nd2wsi)});
    """)
    assert out["unchanged"] is True
    assert out["pending"] is None
    assert out["commands"] == []
    assert out["errors"] == ["Fit protected — use Remove Fit before changing orientation"]


@pytest.mark.parametrize("changes", [
    {"paneInstanceId": "old-pane"}, {"contextEpoch": 0},
    {"spatialContextKey": "old-site"}, {"groupSessionId": "old-group"},
    {"groupEpoch": 0}, {"sid": "a"}, {"version": 1},
])
def test_shell_rejects_stale_or_misattributed_orientation_messages(changes):
    out = run_shell("""
      const data={nd2wsi:'compare-orientation-shortcut',version:2,sid:'b',
        action:'rotate-right',...spatialEnvelope('b'),...CHANGES};
      paneMessage('b',data);
      ({pending:compare.pendingRequest,commands:messages.filter(m=>m.nd2wsi)});
    """.replace("CHANGES", json.dumps(changes)))
    assert out == {"pending": None, "commands": []}


def test_pane_originated_shortcut_keeps_the_shell_active_linked_target():
    out = run_shell("""
      compare.orientationSid='c';
      paneMessage('b',{nd2wsi:'compare-orientation-shortcut',version:2,sid:'b',
        action:'rotate-right',...spatialEnvelope('b')});
      const target=compare.pendingRequest?.targetSid;
      replyAll();
      ({target,pose:displayTransformFor('c'),sender:displayTransformFor('b')});
    """)
    assert out == {"target": "c", "pose": {"degrees": 90, "flipped": False},
                   "sender": {"degrees": 0, "flipped": False}}


def test_shell_without_compare_captures_active_pane_identity_for_forwarding():
    out = run_shell("""
      compare.enabled=false; active='c';
      const consumed=pressKey('f',{code:'KeyF',metaKey:true});
      ({consumed,commands:messages.filter(m=>m.nd2wsi),pending:compare.pendingRequest});
    """)
    assert out == {
        "consumed": {"prevented": True, "stopped": True}, "pending": None,
        "commands": [{"sid": "c", "nd2wsi": "pane-orientation-shortcut", "version": 2,
                      "action": "flip-horizontal", "paneInstanceId": "pane-c",
                      "contextEpoch": 1, "spatialContextKey": "context-c"}],
    }
