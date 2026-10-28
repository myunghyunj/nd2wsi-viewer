"""Production keyboard routing before the bundled OpenSeadragon target listener.

The VM models DOM capture/target/bubble delivery, including cancellation. The
OSD keyboard dispatcher and viewer action handler come from the bundled vendor
file; app classification, ownership, orientation and pane identity guards also
execute their production implementations.
"""

import json
from pathlib import Path

import pytest
from node_host import run_node
from test_compare_orientation_js import run as run_shell

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi" / "static"

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
const out={clicks:[],panels:[],translucent:0,tools:[],messages:[],toasts:[],applied:[],orientationCalls:[],
  osdRotations:[],osdFlips:0,pans:[],zooms:[],homes:0,autofocus:[],
  targetDeliveries:0,osdKeydowns:0,events:[],roiPans:[],roiZooms:[],roiScrolls:[],roiFocus:[]};
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
  quitPreparing:!!config.quitPreparing,roi:null,tool:null,pixel:{cursor:null},tabCount:2,
  landmark:{active:!!config.landmark,points:[]},
  annotations:config.astromap?.items || [],
  astromap:{activeId:config.astromap?.activeId || null,rows:new Map()},
  info:{plate:{Z:config.zCount ?? 5,T:4}},
  plate:config.plate ? {auto:false,focus:config.grid ? null : 0,t:0,placed:[]} : null,
  viewportRelay:{commandGate:gate,compare:config.compare || null,
    displayRotation:config.degrees || 0,displayFlipped:!!config.flipped,
    reopening:false,contextChanging:false},
};
let infoHidden=true;
state.windows={info:config.disabled?.includes('info') ? null : {
  isHidden:()=>infoHidden,
  open(){infoHidden=false;out.panels.push({panel:'info',open:true});},
  close(){infoHidden=true;out.panels.push({panel:'info',open:false});},
}};
if(config.astromap) {
  state.windows.astromap={isHidden:()=>!!config.astromap.hidden};
  for(const item of state.annotations) state.astromap.rows.set(item.id,{
    el:{scrollIntoView:()=>out.roiScrolls.push(item.id)},
    number:{focus:()=>out.roiFocus.push(item.id)},
  });
}
let rotation=config.degrees || 0, flipped=!!config.flipped;
const viewport={
  getRotation:()=>rotation,getFlip:()=>flipped,
  get flipped(){return flipped;},
  setRotation(value){rotation=value;out.osdRotations.push(value);},
  toggleFlip(){flipped=!flipped;out.osdFlips++;},
  applyConstraints(){},deltaPointsFromPixels:p=>p,
  panBy:p=>out.pans.push(p),zoomBy:value=>out.zooms.push(value),goHome:()=>out.homes++,
  getZoom:()=>3.25,zoomTo:(...args)=>out.roiZooms.push(args),
  imageToViewportCoordinates:p=>p,panTo:(...args)=>out.roiPans.push(args),
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
const window={Nd2ShortcutRouter:Router,Nd2ViewerGeometry:require(root+'/viewer-geometry-v1.js'),
  addEventListener(kind,fn,options){
  if(!listeners.has(kind)) listeners.set(kind,[]);
  listeners.get(kind).push({fn,capture:options===true || !!options?.capture});
}};
window.parent=config.iframe ? {postMessage:data=>out.messages.push(data)} : window;
if(config.newWindowNative) window.parent.pywebview={api:{new_window(){}}};
const OpenSeadragon=()=>viewer;
OpenSeadragon.Point=function(x,y){this.x=x;this.y=y;};
const context=vm.createContext({
  state,window,document:{body:element('body')},location:{origin:'http://qa.invalid'},
  VIEWPORT_PROTOCOL_VERSION:2,$:element,structuredClone,
  OpenSeadragon,makeTileSource:()=>({}),
  renderAnnotations(){},updateAlignmentDrag(){},debounceToast:()=>()=>{},
  refreshAstromapPanel(){},activeFrameContext:()=>config.grid ? null : {},
  annotationOwnerReady:()=>config.astromap?.ownerReady!==false,
  refreshSpatialContext(){},postSpatialReadiness(){},currentSlideSid:()=> 'test',
  showToast:message=>out.toasts.push(message),
  openTranslucentViewer(){out.translucent++;},
  setTool:tool=>{state.tool=tool;out.tools.push(tool);},togglePixelInspector(){},
  setPlateAuto:value=>{state.plate.auto=value;out.autofocus.push(value);},
  stepPlateZ(){},setPlateT(){},setPlatePlaying(){},setPlateFocus(){},
  applyDesiredDisplayTransform(){
    rotation=state.viewportRelay.displayRotation;flipped=state.viewportRelay.displayFlipped;
    out.applied.push({degrees:rotation,flipped});return true;
  },
});
for(const name of ['buildViewer','normalizedRotation','spatialIdentity','spatialImageReady',
  'spatialPaneReady','applyOrientationShortcut','togglePanelForShortcut',
  'astromapItems','selectAstromapRoi','panToAstromapRoi','navigateAstromapKey','wireAstromapKeys',
  'wirePlateKeys','wireCompareRelay','wireKeys']) {
  vm.runInContext(production(name),context);
}
const applyOrientation=context.applyOrientationShortcut;
context.applyOrientationShortcut=action=>{
  out.orientationCalls.push(action);return applyOrientation(action);
};
// Keep real viewer keyboard-event hooks and initialization order too.
context.buildViewer();
context.wireAstromapKeys();
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
    ownerDocument:{getSelection:()=>spec.selection === undefined ? null : {
      isCollapsed:!!spec.selectionCollapsed,toString:()=>spec.selection,
    }},
    type:targetSpec.type,
    isContentEditable:!!targetSpec.editable,
    closest(selector){
      if(targetSpec.insideEditable && selector.includes('contenteditable')) return {};
      if(targetSpec.insideTextbox && selector.includes('[role="textbox"]')) return {};
      if(targetSpec.plateMenu && selector.includes('#plate-view-menu')) return {};
      if(targetSpec.astromapRow && selector.includes('#astromap-list')) return {};
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
    origin:config.messageOrigin || 'http://qa.invalid',
    source:config.foreignSource ? {} : window.parent,data,
  });
}
out.pose={degrees:state.viewportRelay.displayRotation,flipped:state.viewportRelay.displayFlipped};
out.activeRoi=state.astromap.activeId;
out.annotations=state.annotations;
process.stdout.write(JSON.stringify(out));
"""


def pane(events=(), **config):
    result = run_node(SCRIPT, STATIC, json.dumps({"events": events, **config}))
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def key(letter, **kwargs):
    return {"key": letter.lower(), "code": "Key" + letter.upper(),
            "keyCode": ord(letter.upper()), **kwargs}


@pytest.mark.parametrize("letter,button,tool", [
    ("E", "tb-region", None), ("P", None, "pin"),
    ("C", "tb-channels", None), ("A", "tb-annot", None), ("R", None, None),
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
    ({"plate": True, "compare": {"enabled": True, "linked": True, "frameLinked": True}}, []),
    ({"plate": True, "compare": {"enabled": True, "linked": True, "frameLinked": False}}, [True]),
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
    ("T", "rotate-right", {"degrees": 90, "flipped": False}),
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


@pytest.mark.parametrize("turn_key", ["T", "R"])
def test_clockwise_rotation_reverses_the_osd_increment_when_mirrored(turn_key):
    out = pane([key(turn_key, metaKey=True)], degrees=0, flipped=True)
    assert out["pose"] == {"degrees": 270, "flipped": True}
    assert out["applied"] == [out["pose"]]


@pytest.mark.parametrize("letter,meta", [("E", False), ("R", False), ("F", False),
                                         ("C", False), ("A", False),
                                         ("T", True), ("R", True), ("F", True), ("C", True),
                                         ("A", True), ("I", True)])
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
    out = pane([key("E", target=target, outsideCanvas=True),
                key("C", target=target, outsideCanvas=True),
                key("A", target=target, outsideCanvas=True),
                key("F", target=target, outsideCanvas=True, metaKey=True),
                key("T", target=target, outsideCanvas=True, metaKey=True),
                key("C", target=target, outsideCanvas=True, metaKey=True),
                key("A", target=target, outsideCanvas=True, metaKey=True),
                key("I", target=target, outsideCanvas=True, metaKey=True)])
    assert out["events"] == [{"prevented": False, "stopped": False}] * 8
    assert out["clicks"] == out["orientationCalls"] == out["applied"] == []


@pytest.mark.parametrize("plate", [False, True])
@pytest.mark.parametrize("letter,button", [("C", "tb-channels"), ("E", "tb-region"), ("A", "tb-annot")])
def test_checkbox_panel_shortcut_runs_and_space_remains_native(plate, letter, button):
    target = {"tag": "INPUT", "type": "checkbox"}
    out = pane([key(letter, target=target, outsideCanvas=True),
                {"key": " ", "code": "Space", "keyCode": 32, "target": target, "outsideCanvas": True}],
               plate=plate)
    assert out["clicks"] == [button]
    assert out["events"] == [{"prevented": True, "stopped": True},
                             {"prevented": False, "stopped": False}]
    assert out["osdKeydowns"] == 0


@pytest.mark.parametrize("overrides", [
    "{target:{tagName:'SELECT',closest:()=>null}}",
    "{target:{tagName:'INPUT',type:'text',closest:()=>null}}",
    "{target:{tagName:'TEXTAREA',closest:()=>null}}",
    "{target:{tagName:'DIV',isContentEditable:true,closest:()=>null}}",
    "{target:{tagName:'SPAN',closest:selector=>selector.includes('contenteditable')?{}:null}}",
    "{isComposing:true}", "{keyCode:229}", "{defaultPrevented:true}",
    "{shiftKey:true}", "{altKey:true}", "{metaKey:true}", "{ctrlKey:true}", "{repeat:true}",
    "{composedPath:()=>[{tagName:'SELECT'}, {tagName:'DIV'}]}",
])
def test_shell_l_preserves_typing_composition_and_prevented_events(overrides):
    out = run_shell(f"""
      $('compare-link-menu').hidden=true;
      const consumed=pressKey('l',{{code:'KeyL',...{overrides}}});
      ({{consumed,menuHidden:$('compare-link-menu').hidden}});
    """)
    assert out["menuHidden"] is True
    assert out["consumed"] == {"prevented": "defaultPrevented:true" in overrides, "stopped": False}


@pytest.mark.parametrize("key_value,target", [
    ("l", "{tagName:'DIV'}"), ("ㅣ", "{tagName:'DIV'}"),
    ("l", "{tagName:'INPUT',type:'checkbox'}"),
])
def test_shell_plain_l_opens_compare_link_menu_from_non_typing_controls(key_value, target):
    out = run_shell(f"""
      $('compare-link-menu').hidden=true;
      const consumed=pressKey({json.dumps(key_value)},{{code:'KeyL',target:{target}}});
      ({{consumed,menuHidden:$('compare-link-menu').hidden}});
    """)
    assert out == {"consumed": {"prevented": True, "stopped": False}, "menuHidden": False}


@pytest.mark.parametrize("letter,korean,button", [("C", "ㅊ", "tb-channels"),
                                                 ("E", "ㄷ", "tb-region"),
                                                 ("A", "ㅁ", "tb-annot")])
def test_ime_composition_remains_unhandled_and_latin_shortcuts_work_afterward(letter, korean, button):
    out = pane([key(letter, key=korean, keyCode=229, isComposing=True),
                key(letter, key=korean)])
    assert out["events"] == [{"prevented": False, "stopped": False},
                             {"prevented": True, "stopped": True}]
    assert out["clicks"] == [button]
    assert out["osdRotations"] == []


@pytest.mark.parametrize("platform", ["MacIntel", "Win32"])
@pytest.mark.parametrize("letter,korean,button", [("C", "ㅊ", "tb-channels"),
                                                 ("E", "ㄷ", "tb-region"),
                                                 ("A", "ㅁ", "tb-annot")])
def test_plain_panels_run_once_before_osd_in_both_keyboard_layouts(platform, letter, korean, button):
    out = pane([key(letter), key(letter, repeat=True), key(letter, key=korean),
                key(letter, key=korean, repeat=True)], platform=platform)
    assert out["clicks"] == [button, button]
    assert out["events"] == [{"prevented": True, "stopped": True}] * 4
    assert out["targetDeliveries"] == out["osdKeydowns"] == 0
    assert out["osdRotations"] == out["pans"] == []


@pytest.mark.parametrize("letter,button", [("C", "tb-channels"), ("A", "tb-annot"),
                                          ("I", "info")])
@pytest.mark.parametrize("platform,modifier", [("MacIntel", "metaKey"), ("Win32", "ctrlKey")])
def test_command_panels_run_once_before_osd(letter, button, platform, modifier):
    out = pane([key(letter, **{modifier: True}),
                key(letter, repeat=True, **{modifier: True})], platform=platform)
    assert out["clicks"] == ([] if button == "info" else [button])
    assert out["panels"] == ([{"panel": "info", "open": True}] if button == "info" else [])
    assert out["events"] == [{"prevented": True, "stopped": True}] * 2
    assert out["targetDeliveries"] == out["osdKeydowns"] == 0
    assert out["osdRotations"] == out["pans"] == []


@pytest.mark.parametrize("letter,panel", [("C", "channels"), ("A", "annot"),
                                        ("E", "region"), ("I", "info")])
@pytest.mark.parametrize("platform,modifier", [("MacIntel", "metaKey"), ("Win32", "ctrlKey")])
def test_shell_panel_shortcuts_reach_only_the_active_tab_once(letter, panel, platform, modifier):
    out = run_shell(f"""
      active='a'; compare.orientationSid='c';
      const flags={{code:'Key{letter}',{modifier}:{str(letter != 'E').lower()},
        target:{{tagName:'BUTTON',closest:()=>null}}}};
      const first=pressKey('{letter.lower()}',flags);
      const held=pressKey('{letter.lower()}',{{...flags,repeat:true}});
      ({{first,held,messages}});
    """, platform=platform)
    assert out["first"] == out["held"] == {"prevented": True, "stopped": True}
    assert out["messages"] == [{
        "sid": "a", "nd2wsi": "pane-panel-shortcut", "version": 2, "panel": panel,
        "paneInstanceId": "pane-a", "contextEpoch": 1, "spatialContextKey": "context-a",
    }]


@pytest.mark.parametrize("platform", ["MacIntel", "Win32"])
@pytest.mark.parametrize("letter,korean,panel", [("C", "ㅊ", "channels"),
                                                ("E", "ㄷ", "region"),
                                                ("A", "ㅁ", "annot")])
def test_shell_plain_panels_target_active_tab_from_buttons_and_checkboxes(platform, letter, korean, panel):
    out = run_shell(f"""
      active='a'; compare.orientationSid='c';
      const flags={{code:'Key{letter}',target:{{tagName:'BUTTON',closest:()=>null}}}};
      const first=pressKey('{letter.lower()}',flags);
      const held=pressKey('{letter.lower()}',{{...flags,repeat:true}});
      const korean=pressKey({json.dumps(korean)},{{...flags,
        target:{{tagName:'INPUT',type:'checkbox',closest:()=>null}}}});
      ({{first,held,korean,messages}});
    """, platform=platform)
    assert out["first"] == out["held"] == out["korean"] == {"prevented": True, "stopped": True}
    assert out["messages"] == [{
        "sid": "a", "nd2wsi": "pane-panel-shortcut", "version": 2, "panel": panel,
        "paneInstanceId": "pane-a", "contextEpoch": 1, "spatialContextKey": "context-a",
    }] * 2


@pytest.mark.parametrize("letter", ["C", "E", "A"])
def test_shell_plain_panel_letters_preserve_text_entry_ime_and_unowned_modifiers(letter):
    out = run_shell(f"""
      const events=[{{target:{{tagName:'INPUT'}}}},{{target:{{tagName:'TEXTAREA'}}}},
        {{target:{{isContentEditable:true}}}},{{isComposing:true}},{{keyCode:229}},
        {{altKey:true}},{{shiftKey:true}}].map(flags=>
          pressKey('{letter.lower()}',{{code:'Key{letter}',...flags}}));
      ({{events,messages}});
    """)
    assert out["events"] == [{"prevented": False, "stopped": False}] * 7
    assert out["messages"] == []


@pytest.mark.parametrize("letter", ["C", "A", "E", "I"])
@pytest.mark.parametrize("overrides", [
    "target:{tagName:'INPUT'}", "target:{tagName:'TEXTAREA'}",
    "target:{isContentEditable:true}", "isComposing:true", "keyCode:229",
    "defaultPrevented:true", "altKey:true", "shiftKey:true",
])
def test_shell_panel_shortcuts_preserve_editing_and_route_only_owned_chords(letter, overrides):
    out = run_shell(f"""
      const consumed=pressKey('{letter.lower()}',{{code:'Key{letter}',
        metaKey:{str(letter != 'E').lower()},{overrides}}});
      ({{consumed,messages}});
    """)
    if letter == "A" and overrides == "shiftKey:true":
        assert out["messages"] == [{
            "sid": "a", "nd2wsi": "astromap-toggle", "version": 2, "open": False,
            "paneInstanceId": "pane-a", "contextEpoch": 1, "spatialContextKey": "context-a",
        }]
        assert out["consumed"] == {"prevented": True, "stopped": True}
        return
    assert out["messages"] == []
    assert out["consumed"] == {"prevented": overrides == "defaultPrevented:true", "stopped": False}


@pytest.mark.parametrize("letter", ["C", "A"])
def test_shell_panel_shortcuts_preserve_native_copy_and_select_all_for_selected_text(letter):
    out = run_shell(f"""
      const consumed=pressKey('{letter.lower()}',{{code:'Key{letter}',metaKey:true,
        target:{{tagName:'DIV',ownerDocument:{{getSelection:()=>({{
          isCollapsed:false,toString:()=> 'selected metadata',
        }})}}}}}});
      ({{consumed,messages}});
    """)
    assert out == {"consumed": {"prevented": False, "stopped": False}, "messages": []}


@pytest.mark.parametrize("setup", [
    "active=null", "readyFrames.delete(active)", "compare.states.delete(active)",
    "pairPicker.open=true", "quitPreparation={}",
])
def test_shell_does_not_send_panel_keys_to_missing_unready_or_modal_panes(setup):
    out = run_shell(f"{setup}; pressKey('e',{{code:'KeyE'}}); messages;")
    assert out == []


def test_shell_panel_forwarding_also_works_without_compare_or_a_ready_image():
    out = run_shell("""
      compare.enabled=false;
      Object.assign(compare.states.get(active),{imageReady:false,spatialContext:null,plateGrid:true});
      pressKey('i',{code:'KeyI',metaKey:true}); messages;
    """)
    assert len(out) == 1 and out[0]["panel"] == "info"
    assert out[0]["sid"] == "a" and out[0]["spatialContextKey"] is None


@pytest.mark.parametrize("panel,button", [("channels", "tb-channels"), ("annot", "tb-annot"),
                                        ("region", "tb-region"), ("info", "info")])
@pytest.mark.parametrize("config,accepted", [
    ({}, True), ({"compare": {"enabled": True, "linked": True}}, True),
    ({"plate": True, "grid": True}, True), ({"changeContext": True}, False),
    ({"message": {"paneInstanceId": "old-pane"}}, False),
    ({"message": {"contextEpoch": 0}}, False),
    ({"message": {"spatialContextKey": "old-site"}}, False),
    ({"message": {"version": 1}}, False),
    ({"messageOrigin": "http://other.invalid"}, False), ({"foreignSource": True}, False),
    ({"quitPreparing": True}, False),
])
def test_shell_panel_message_reaches_only_its_current_trusted_pane(panel, button, config, accepted):
    config = dict(config)
    message = {"nd2wsi": "pane-panel-shortcut", "panel": panel, **config.pop("message", {})}
    out = pane(iframe=True, message=message, **config)
    assert out["clicks"] == ([button] if accepted and panel != "info" else [])
    assert out["panels"] == ([{"panel": "info", "open": True}] if accepted and panel == "info" else [])


@pytest.mark.parametrize("panel", ["unknown", "toString", "__proto__", None])
def test_shell_panel_messages_do_not_dispatch_arbitrary_buttons(panel):
    out = pane(iframe=True, message={"nd2wsi": "pane-panel-shortcut", "panel": panel})
    assert out["clicks"] == []


def test_disabled_panel_stays_disabled_for_forwarded_keys():
    out = pane(iframe=True, disabled=["tb-region"],
               message={"nd2wsi": "pane-panel-shortcut", "panel": "region"})
    assert out["clicks"] == []


@pytest.mark.parametrize("letter", ["C", "A"])
@pytest.mark.parametrize("platform,modifier", [("MacIntel", "metaKey"), ("Win32", "ctrlKey")])
def test_selected_read_only_text_keeps_native_copy_and_select_all(letter, platform, modifier):
    out = pane([key(letter, selection="Slide metadata", outsideCanvas=True,
                    **{modifier: True})], platform=platform)
    assert out["clicks"] == []
    assert out["events"] == [{"prevented": False, "stopped": False}]


@pytest.mark.parametrize("letter,button", [("C", "tb-channels"), ("A", "tb-annot"),
                                          ("I", "info")])
def test_disabled_command_panel_stays_reserved_without_toggling(letter, button):
    out = pane([key(letter, metaKey=True)], disabled=[button])
    assert out["clicks"] == []
    assert out["events"] == [{"prevented": True, "stopped": True}]
    assert out["osdKeydowns"] == 0


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
    ("t", {"degrees": 90, "flipped": False}),
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
@pytest.mark.parametrize("turn_key", ["T", "R"])
def test_shell_orientation_uses_strict_command_modifiers_and_typing_guards(overrides, turn_key):
    out = run_shell(f"""
      const consumed=pressKey('{turn_key.lower()}',{{code:'Key{turn_key}',...{overrides}}});
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


def test_info_shortcut_opens_and_closes_the_inspector_without_proxy_button():
    out = pane([key("I", metaKey=True), key("I", metaKey=True)])
    assert out["clicks"] == []
    assert out["panels"] == [{"panel": "info", "open": True}, {"panel": "info", "open": False}]


@pytest.mark.parametrize("config,accepted", [
    ({}, True), ({"changeContext": True}, False),
    ({"message": {"paneInstanceId": "old-pane"}}, False),
    ({"message": {"contextEpoch": 0}}, False),
    ({"message": {"spatialContextKey": "old-site"}}, False),
    ({"message": {"sid": "different-slide"}}, False),
    ({"message": {"version": 1}}, False),
    ({"messageOrigin": "http://other.invalid"}, False), ({"foreignSource": True}, False),
])
def test_cloud_command_reaches_only_the_current_trusted_pane(config, accepted):
    config = dict(config)
    message = {"nd2wsi": "pane-translucent-open", "sid": "test", **config.pop("message", {})}
    out = pane(iframe=True, message=message, **config)
    assert out["translucent"] == int(accepted)


@pytest.mark.parametrize("target", [{"tag": "DIV"}, {"tag": "INPUT"}, {"tag": "TEXTAREA"}])
def test_windows_pane_new_window_shortcut_forwards_once_before_typing_guards(target):
    out = pane([key("N", ctrlKey=True, target=target), key("N", ctrlKey=True, repeat=True, target=target),
                key("N", ctrlKey=True, shiftKey=True, target=target)],
               platform="Win32", iframe=True, newWindowNative=True)
    assert [message["role"] for message in out["messages"]] == ["user", "agent"]
    assert all(message["nd2wsi"] == "new-window-shortcut" and message["sid"] == "test" and
               message["paneInstanceId"] == "pane-test" for message in out["messages"])
    assert out["events"] == [{"prevented": True, "stopped": True}] * 3


@pytest.mark.parametrize("config,flags", [
    ({"platform": "MacIntel"}, {"metaKey": True}),
    ({"platform": "Win32"}, {"metaKey": True}),
    ({"platform": "Win32"}, {"ctrlKey": True, "altKey": True}),
    ({"platform": "Win32"}, {"ctrlKey": True, "isComposing": True, "keyCode": 229}),
    ({"platform": "Win32", "newWindowNative": False}, {"ctrlKey": True}),
])
def test_pane_does_not_duplicate_native_mac_or_unsupported_new_window_actions(config, flags):
    out = pane([key("N", **flags)], iframe=True, **{"newWindowNative": True, **config})
    assert out["messages"] == []
