"""Real LUT toolbar handlers: exact editing, clipboard routing and safe updates."""
import json
import re
import shutil
import subprocess
from pathlib import Path
from xml.etree import ElementTree

import pytest

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi/static"
NODE = shutil.which("node")

HARNESS = r"""
const fs=require('fs'),path=require('path'),vm=require('vm');
const source=fs.readFileSync(path.join(process.argv[1],'app.js'),'utf8');
const transfer=require(path.join(process.argv[1],'lut-transfer-v1.js'));
const nodes={},created=[],updates=[],toasts=[],writes=[];
let flushes=0,browserReads=0,nativeReads=0;
let clock=0,nextTimer=1;
const timers=new Map();
function setTimeout(fn,delay){const id=nextTimer++;timers.set(id,{fn,due:clock+delay});return id}
function clearTimeout(id){timers.delete(id)}
function advance(ms){
  const end=clock+ms;
  while(true){
    const next=[...timers].filter(([,timer])=>timer.due<=end).sort((a,b)=>a[1].due-b[1].due)[0];
    if(!next)break;
    clock=next[1].due;timers.delete(next[0]);next[1].fn();
  }
  clock=end;
}
function eventTarget(target={}){
  target.handlers={};
  target.addEventListener=function(key,fn){this.handlers[key]=fn};
  target.removeEventListener=function(key,fn){if(this.handlers[key]===fn)delete this.handlers[key]};
  return target;
}
function node(tag){
  const classes=new Set();
  const result=eventTarget({tag,children:[],attrs:{},style:{},hidden:false,disabled:false,textContent:'',value:'',
    offsetWidth:180,offsetHeight:28,
    classList:{add(...names){names.forEach(name=>classes.add(name))},
      remove(...names){names.forEach(name=>classes.delete(name))},contains(name){return classes.has(name)},
      toggle(name,force){const value=force===undefined?!classes.has(name):force;
        if(value)classes.add(name);else classes.delete(name);return value}},
    getBoundingClientRect(){return this.rect||{left:100,top:50,right:128,bottom:78,width:28,height:28}},
    append(...children){this.children.push(...children)},
    replaceChildren(...children){this.children=[...children]},
    setAttribute(key,value){this.attrs[key]=value},
    focus(){this.focused=true},
    showModal(){this.open=true},close(){this.open=false}});
  Object.defineProperty(result,'valueAsNumber',{get(){return this.value.trim()==='' ? NaN : Number(this.value)}});
  created.push(result);return result;
}
for(const id of ['lut-type','lut-copy','lut-paste','lut-copy-tooltip','lut-dialog','lut-fields','lut-error','lut-form','lut-cancel'])nodes[id]=node(id);
nodes['lut-copy-tooltip'].hidden=true;
nodes['lut-copy-tooltip'].textContent='Copy LUT to clipboard';
nodes['lut-copy-tooltip'].rect={left:0,top:0,right:180,bottom:28,width:180,height:28};
const channel=(label,start=0,end=65535)=>({label,window:{start,end}});
const state={info:{dtype:'uint16',channels:[channel('CY5',129,672),channel('DAPI',0,0.75),channel('FITC',15,200)]},
  luts:[{lo:127.25,hi:4363.75,gamma:0.37},null,{lo:20,hi:190,gamma:1.5}],
  lutAutoRange:true,enabled:[true,false,true],lutWidgets:[]};
state.lutWidgets=state.info.channels.map((ch,index)=>({setLut(lut){
  const value={...lut};updates.push({index,lut:value});state.luts[index]=value;
}}));
const window=eventTarget({Nd2LutTransfer:transfer,parent:{},innerWidth:800,innerHeight:600,setTimeout,clearTimeout});
const navigator={clipboard:{readText:async()=>{browserReads++;return clipboardText}}};
let clipboardText='';
const document=eventTarget({createElement:node});
const context={state,window,navigator,document,setTimeout,clearTimeout,$:id=>nodes[id],
  applyLuts:{flush:()=>flushes++},showToast:text=>toasts.push(text),
  copyPlainText:async text=>writes.push({transport:'browser',text})};
vm.createContext(context);
const start=source.indexOf('function wireLutToolbar(');
const end=source.indexOf('/* ---- per-channel LUT',start);
if(start<0||end<0)throw new Error('LUT toolbar source bounds not found');
vm.runInContext(source.slice(start,end),context);
context.wireLutToolbar();
const copyFeedback=()=>({copied:nodes['lut-copy'].classList.contains('is-copied'),
  hidden:nodes['lut-copy-tooltip'].hidden,text:nodes['lut-copy-tooltip'].textContent,
  disabled:nodes['lut-copy'].disabled,timers:timers.size});
const editorInputs=()=>nodes['lut-fields'].children.map(row=>row.children[1].children.map(label=>label.children[0]));
const setValues=values=>editorInputs().forEach((inputs,i)=>inputs.forEach((input,j)=>input.value=String(values[i][j])));
const snapshot=()=>JSON.stringify({info:state.info,luts:state.luts,enabled:state.enabled,lutAutoRange:state.lutAutoRange});
const submit=()=>nodes['lut-form'].onsubmit({preventDefault(){}});
const payload=channels=>JSON.stringify({format:'nd2wsi-lut/1',dtype:'uint16',channels});
const entry=(label,lo,hi,gamma=1)=>({label,lo,hi,gamma});
const installNative=(platform='cocoa')=>window.parent.pywebview={platform,api:{
  write_lut_clipboard:async text=>{writes.push({transport:'native',text});return {ok:true}},
  read_lut_clipboard:async()=>{nativeReads++;return {ok:true,text:clipboardText}}
}};
"""


def run(body):
    if NODE is None:
        pytest.skip("node is not installed")
    result = subprocess.run(
        [NODE, "-e", HARNESS + "(async()=>{" + body +
         "})().then(result=>process.stdout.write(JSON.stringify(result))).catch(error=>{console.error(error);process.exitCode=1});", str(STATIC)],
        check=True, capture_output=True, encoding="utf-8", timeout=20,
    )
    return json.loads(result.stdout)


def test_type_lut_opens_accessible_exact_value_fields_and_isolates_editor_keys():
    out = run("""
const before=snapshot();nodes['lut-type'].onclick();
let stopped=false;nodes['lut-dialog'].handlers.keydown({stopPropagation(){stopped=true}});
return {open:nodes['lut-dialog'].open,stopped,unchanged:before===snapshot(),
  names:nodes['lut-fields'].children.map(row=>row.children[0].textContent),
  fields:editorInputs().map(inputs=>inputs.map(input=>({type:input.type,step:input.step,required:input.required,
    value:input.value,label:input.attrs['aria-label'],min:input.min,max:input.max,focused:!!input.focused})))};
""")
    assert out["open"] is out["stopped"] is out["unchanged"] is True
    assert out["names"] == ["CY5", "DAPI", "FITC"]
    assert [[field["value"] for field in channel] for channel in out["fields"]] == [
        ["127.25", "4363.75", "0.37"], ["0", "0.75", "1"], ["20", "190", "1.5"],
    ]
    for name, fields in zip(out["names"], out["fields"]):
        for caption, field in zip(["Minimum", "Maximum", "Gamma"], fields):
            assert field["type"] == "number" and field["step"] == "any" and field["required"] is True
            assert field["label"] == f"{name} {caption}"
        assert (fields[2]["min"], fields[2]["max"]) == ("0.25", "4")
    assert out["fields"][0][0]["focused"] is True


@pytest.mark.parametrize("invalid", [[5, 5, 1], [10, 5, 1], [0, 10, 0.24], [0, 10, 4.01], ["", 10, 1], [0, 0.5, 1]])
def test_invalid_numeric_apply_is_all_or_nothing(invalid):
    out = run("""
const before=snapshot();nodes['lut-type'].onclick();
setValues([[100,500,0.5],INVALID,[20,190,1.5]]);submit();
return {unchanged:before===snapshot(),updates,flushes,open:nodes['lut-dialog'].open,error:nodes['lut-error'].textContent,toasts};
""".replace("INVALID", json.dumps(invalid)))
    assert out["unchanged"] is True
    assert out["updates"] == [] and out["flushes"] == 0 and out["toasts"] == []
    assert out["open"] is True and out["error"].startswith("DAPI:")


def test_cancel_discards_edits_and_reopen_rebuilds_from_current_state():
    out = run("""
const before=snapshot();nodes['lut-type'].onclick();
setValues([[1,100,1],[2,200,2],[3,300,3]]);nodes['lut-cancel'].onclick();
const closed=!nodes['lut-dialog'].open;nodes['lut-type'].onclick();
return {closed,unchanged:before===snapshot(),updates,flushes,
  values:editorInputs().map(inputs=>inputs.map(input=>input.value))};
""")
    assert out["closed"] is out["unchanged"] is True
    assert out["updates"] == [] and out["flushes"] == 0
    assert out["values"] == [["127.25", "4363.75", "0.37"], ["0", "0.75", "1"], ["20", "190", "1.5"]]


def test_valid_numeric_apply_sets_each_channel_once_and_flushes_once():
    out = run("""
const metadata=JSON.stringify(state.info);nodes['lut-type'].onclick();
setValues([[100.25,500.75,0.5],[0,0.75,1],[3,300,4]]);submit();
return {updates,flushes,open:nodes['lut-dialog'].open,toasts,
  metadataPreserved:metadata===JSON.stringify(state.info),enabled:state.enabled,auto:state.lutAutoRange};
""")
    assert out["updates"] == [
        {"index": 0, "lut": {"lo": 100.25, "hi": 500.75, "gamma": 0.5}},
        {"index": 1, "lut": {"lo": 0, "hi": 0.75, "gamma": 1}},
        {"index": 2, "lut": {"lo": 3, "hi": 300, "gamma": 4}},
    ]
    assert out["flushes"] == 1 and out["open"] is False
    assert out["toasts"] == ["LUT values applied"]
    assert out["metadataPreserved"] is out["auto"] is True
    assert out["enabled"] == [True, False, True]


@pytest.mark.parametrize("platform", ["cocoa", "edgechromium"])
def test_native_copy_and_reordered_partial_paste_use_native_bridge_and_preserve_unmatched(platform):
    out = run("""
installNative(PLATFORM);await nodes['lut-copy'].onclick();
clipboardText=payload([entry('DAPI',10,100,0.5),entry('OTHER',3,30),entry('CY5',20,200,2)]);
await nodes['lut-paste'].onclick();
return {writes:writes.map(write=>({...write,payload:JSON.parse(write.text),text:undefined})),
  nativeReads,browserReads,updates,flushes,luts:state.luts,enabled:state.enabled,auto:state.lutAutoRange,
  toasts,buttons:[nodes['lut-copy'].disabled,nodes['lut-paste'].disabled]};
""".replace("PLATFORM", json.dumps(platform)))
    assert out["writes"] == [{"transport": "native", "payload": {
        "format": "nd2wsi-lut/1", "dtype": "uint16", "channels": [
            {"label": "CY5", "lo": 127.25, "hi": 4363.75, "gamma": 0.37},
            {"label": "DAPI", "lo": 0, "hi": 0.75, "gamma": 1},
            {"label": "FITC", "lo": 20, "hi": 190, "gamma": 1.5},
        ],
    }}]
    assert out["nativeReads"] == 1 and out["browserReads"] == 0
    assert out["updates"] == [
        {"index": 1, "lut": {"lo": 10, "hi": 100, "gamma": 0.5}},
        {"index": 0, "lut": {"lo": 20, "hi": 200, "gamma": 2}},
    ]
    assert out["luts"][2] == {"lo": 20, "hi": 190, "gamma": 1.5}
    assert out["enabled"] == [True, False, True] and out["auto"] is True
    assert out["flushes"] == 1 and out["buttons"] == [False, False]
    assert out["toasts"] == ["Channel LUTs copied", "Pasted 2 channel LUTs · 1 skipped"]


def test_browser_clipboard_fallback_succeeds_outside_supported_native_platforms():
    out = run("""
installNative();window.parent.pywebview.platform='qt';
await nodes['lut-copy'].onclick();
clipboardText=payload([entry('CY5',11,111,0.7)]);await nodes['lut-paste'].onclick();
return {transports:writes.map(write=>write.transport),nativeReads,browserReads,updates,flushes,toasts,
  buttons:[nodes['lut-copy'].disabled,nodes['lut-paste'].disabled]};
""")
    assert out["transports"] == ["browser"]
    assert out["nativeReads"] == 0 and out["browserReads"] == 1
    assert out["updates"] == [{"index": 0, "lut": {"lo": 11, "hi": 111, "gamma": 0.7}}]
    assert out["flushes"] == 1 and out["buttons"] == [False, False]
    assert out["toasts"] == ["Channel LUTs copied", "Pasted 1 channel LUT"]


@pytest.mark.parametrize("setup, message", [
    ("clipboardText='not json';", "valid LUT JSON"),
    ("clipboardText=payload([entry('UNKNOWN',1,100)]);", "No compatible"),
    ("navigator.clipboard.readText=async()=>{throw new Error('Permission denied')};", "Permission denied"),
    ("installNative();window.parent.pywebview.api.read_lut_clipboard=async()=>({ok:false,message:'Native read failed'});", "Native read failed"),
    ("installNative('edgechromium');window.parent.pywebview.api.read_lut_clipboard=async()=>({ok:false,message:'Native read failed'});", "Native read failed"),
])
def test_unusable_clipboard_has_no_update_and_reenables_paste(setup, message):
    out = run(setup + """
const before=snapshot();await nodes['lut-paste'].onclick();
return {unchanged:before===snapshot(),updates,flushes,disabled:nodes['lut-paste'].disabled,toasts};
""")
    assert out["unchanged"] is True and out["disabled"] is False
    assert out["updates"] == [] and out["flushes"] == 0
    assert len(out["toasts"]) == 1 and message in out["toasts"][0]


@pytest.mark.parametrize("platform", ["cocoa", "edgechromium"])
def test_failed_native_copy_reports_failure_and_reenables_copy_without_browser_retry(platform):
    out = run("""
installNative(PLATFORM);window.parent.pywebview.api.write_lut_clipboard=async()=>({ok:false,message:'Native write failed'});
await nodes['lut-copy'].onclick();
return {writes,disabled:nodes['lut-copy'].disabled,toasts,updates,flushes};
""".replace("PLATFORM", json.dumps(platform)))
    assert out == {"writes": [], "disabled": False, "toasts": ["Native write failed"], "updates": [], "flushes": 0}


@pytest.mark.parametrize("action, method, message", [
    ("copy", "write_lut_clipboard", "Native LUT clipboard writing is unavailable"),
    ("paste", "read_lut_clipboard", "Native LUT clipboard reading is unavailable"),
])
def test_windows_missing_native_method_does_not_fall_back_to_browser(action, method, message):
    out = run("""
installNative('edgechromium');delete window.parent.pywebview.api[METHOD];
const before=snapshot();await nodes['lut-'+ACTION].onclick();
return {writes,nativeReads,browserReads,unchanged:before===snapshot(),updates,flushes,toasts,
  disabled:nodes['lut-'+ACTION].disabled};
""".replace("METHOD", json.dumps(method)).replace("ACTION", json.dumps(action)))
    assert out == {"writes": [], "nativeReads": 0, "browserReads": 0, "unchanged": True,
                   "updates": [], "flushes": 0, "toasts": [message], "disabled": False}


def test_pending_paste_disables_button_and_ignores_read_after_slide_replacement():
    out = run("""
let finishRead;navigator.clipboard.readText=()=>new Promise(resolve=>finishRead=resolve);
const pending=nodes['lut-paste'].onclick();const disabledDuringRead=nodes['lut-paste'].disabled;
state.info={...state.info,channels:[...state.info.channels]};
const before=snapshot();finishRead(payload([entry('CY5',11,111,0.7)]));await pending;
return {disabledDuringRead,disabled:nodes['lut-paste'].disabled,unchanged:before===snapshot(),updates,flushes,toasts};
""")
    assert out == {"disabledDuringRead": True, "disabled": False, "unchanged": True, "updates": [], "flushes": 0, "toasts": []}


@pytest.mark.parametrize("transport", ["native", "browser"])
def test_copy_feedback_waits_for_confirmed_success_then_expires(transport):
    out = run("""
let resolveCopy;const clipboardPending=new Promise(resolve=>resolveCopy=resolve);
if(TRANSPORT==='native'){
  installNative();window.parent.pywebview.api.write_lut_clipboard=()=>clipboardPending;
}else context.copyPlainText=()=>clipboardPending;
nodes['lut-copy'].handlers.pointerenter();const before=copyFeedback();
const pending=nodes['lut-copy'].onclick();const waiting=copyFeedback();
resolveCopy({ok:true});await pending;const succeeded=copyFeedback();
advance(1799);const almostExpired=copyFeedback();advance(1);const expired=copyFeedback();
nodes['lut-copy'].handlers.pointerleave();
return {before,waiting,succeeded,almostExpired,expired,left:copyFeedback(),toasts};
""".replace("TRANSPORT", json.dumps(transport)))
    assert out["before"] == {
        "copied": False, "hidden": False, "text": "Copy LUT to clipboard", "disabled": False, "timers": 0,
    }
    assert out["waiting"] == {**out["before"], "disabled": True}
    assert out["succeeded"] == out["almostExpired"] == {
        "copied": True, "hidden": False, "text": "Copied!", "disabled": False, "timers": 1,
    }
    assert out["expired"] == out["before"]
    assert out["left"] == {**out["before"], "hidden": True}
    assert out["toasts"] == ["Channel LUTs copied"]


@pytest.mark.parametrize("transport", ["native", "browser"])
def test_failed_copy_clears_prior_success_and_never_displays_a_false_checkmark(transport):
    out = run("""
if(TRANSPORT==='native')installNative();
await nodes['lut-copy'].onclick();const prior=copyFeedback();
let resolveCopy,rejectCopy;
const clipboardPending=new Promise((resolve,reject)=>{resolveCopy=resolve;rejectCopy=reject});
if(TRANSPORT==='native')window.parent.pywebview.api.write_lut_clipboard=()=>clipboardPending;
else context.copyPlainText=()=>clipboardPending;
const pending=nodes['lut-copy'].onclick();const waiting=copyFeedback();
if(TRANSPORT==='native')resolveCopy({ok:false,message:'Copy denied'});
else rejectCopy(new Error('Copy denied'));
await pending;const failed=copyFeedback();advance(2000);
return {prior,waiting,failed,later:copyFeedback(),toasts};
""".replace("TRANSPORT", json.dumps(transport)))
    assert out["prior"]["copied"] is True and out["prior"]["timers"] == 1
    assert out["waiting"] == {
        "copied": False, "hidden": True, "text": "Copy LUT to clipboard", "disabled": True, "timers": 0,
    }
    assert out["failed"] == out["later"] == {**out["waiting"], "disabled": False}
    assert out["toasts"] == ["Channel LUTs copied", "Copy denied"]


def test_repeated_copy_restarts_the_success_lifetime_without_duplicate_timers():
    out = run("""
await nodes['lut-copy'].onclick();advance(900);await nodes['lut-copy'].onclick();
const repeated=copyFeedback();advance(900);const originalDeadline=copyFeedback();
advance(899);const almostExpired=copyFeedback();advance(1);
return {repeated,originalDeadline,almostExpired,expired:copyFeedback()};
""")
    assert out["repeated"] == out["originalDeadline"] == out["almostExpired"] == {
        "copied": True, "hidden": False, "text": "Copied!", "disabled": False, "timers": 1,
    }
    assert out["expired"] == {
        "copied": False, "hidden": True, "text": "Copy LUT to clipboard", "disabled": False, "timers": 0,
    }


def test_tooltip_hover_focus_escape_and_positioning_stay_independent_of_panel_clipping():
    out = run("""
const button=nodes['lut-copy'],tip=nodes['lut-copy-tooltip'];const visible=[];
const record=()=>visible.push(!tip.hidden);
button.handlers.pointerenter();record();button.handlers.pointerleave();record();
button.handlers.focus();record();button.handlers.blur();record();
button.handlers.pointerenter();button.handlers.focus();button.handlers.pointerleave();record();
document.handlers.keydown({key:'Escape'});record();window.handlers.resize();record();
button.handlers.blur();button.handlers.focus();record();button.handlers.blur();
await button.onclick();button.handlers.pointerleave();record();
document.handlers.keydown({key:'Escape'});record();
button.handlers.pointerenter();record();
const afterEscape=copyFeedback();
window.innerWidth=320;window.innerHeight=240;
button.rect={left:300,top:220,right:320,bottom:240,width:20,height:20};
window.handlers.resize();const corner={...tip.style};
button.rect={left:0,top:0,right:20,bottom:20,width:20,height:20};
window.handlers.scroll();const topCorner={...tip.style};
document.handlers.keydown({key:'Escape'});await button.onclick();
return {visible,afterEscape,corner,topCorner,newSuccess:copyFeedback()};
""")
    assert out["visible"] == [True, False, True, False, True, False, False, True, True, False, True]
    assert out["afterEscape"]["copied"] is True and out["afterEscape"]["text"] == "Copied!"
    assert out["corner"] == {"left": "132px", "top": "184px"}
    assert out["topCorner"] == {"left": "8px", "top": "28px"}
    assert out["newSuccess"]["hidden"] is False and out["newSuccess"]["copied"] is True


@pytest.mark.parametrize("pending_copy", [False, True])
def test_pagehide_disposes_feedback_timer_and_ignores_late_success(pending_copy):
    out = run("""
let finishCopy,pending;
if(PENDING_COPY){
  context.copyPlainText=()=>new Promise(resolve=>finishCopy=resolve);
  pending=nodes['lut-copy'].onclick();
}else await nodes['lut-copy'].onclick();
window.handlers.pagehide();
const disposed=copyFeedback();
if(PENDING_COPY){finishCopy();await pending}else nodes['lut-copy'].lutCopyFeedback.succeed();
advance(5000);
return {disposed,later:copyFeedback(),listeners:{keydown:!!document.handlers.keydown,
  resize:!!window.handlers.resize,scroll:!!window.handlers.scroll}};
""".replace("PENDING_COPY", json.dumps(pending_copy)))
    assert out["disposed"] == {
        "copied": False, "hidden": True, "text": "Copy LUT to clipboard", "disabled": pending_copy, "timers": 0,
    }
    assert out["later"] == {**out["disposed"], "disabled": False}
    assert out["listeners"] == {"keydown": False, "resize": False, "scroll": False}


def test_toolbar_html_has_short_auto_fit_label_and_named_controls_in_order():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert re.search(r'id="lut-auto-range"\s*>\s*Auto-fit\s*</label>', html)
    assert "Auto-fit LUT range" not in html
    assert re.search(r'<button\b[^>]*id="lut-type"[^>]*>Type LUT</button>', html)
    for name, symbol in [("Copy", "copy"), ("Paste", "clipboard-paste")]:
        button = re.search(rf'<button\b[^>]*id="lut-{name.lower()}"[^>]*>(.*?)</button>', html)
        assert button is not None
        assert f'aria-label="{name} LUT"' in button.group(0)
        classes = re.search(r'class="([^"]+)"', button.group(0)).group(1).split()
        assert "lut-icon" in classes and "btn" not in classes  # no inherited capsule styling
        if name == "Copy":
            assert "lut-copy" in classes and 'aria-describedby="lut-copy-tooltip"' in button.group(0)
            assert " title=" not in button.group(0)  # custom tooltip owns success feedback
        assert re.fullmatch(
            rf'<span class="lut-symbol lut-{name.lower()}-symbol" data-symbol="{symbol}" aria-hidden="true"></span>',
            button.group(1),
        )
    assert html.index('id="lut-type"') < html.index('id="lut-copy"') < html.index('id="lut-paste"')
    assert html.index("lut-transfer-v1.js") < html.index("app.js")
    for element in ["lut-dialog", "lut-fields", "lut-error", "lut-form", "lut-cancel"]:
        assert f'id="{element}"' in html
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    icon_rule = re.search(r'\.lut-toolbar \.lut-icon\s*\{([^}]+)\}', css).group(1)
    for declaration in ["width: 28px", "height: 28px", "padding: 6px", "appearance: none",
                        "border: 0", "background: transparent", "box-shadow: none", "color: var(--label-2)"]:
        assert declaration in icon_rule
    hover_rule = re.search(r'\.lut-toolbar \.lut-icon:hover\s*\{([^}]+)\}', css).group(1)
    assert "color: var(--label)" in hover_rule and "background" not in hover_rule
    assert ".lut-icon:focus-visible" in css
    symbol_rule = re.search(r'\.lut-icon \.lut-symbol\s*\{([^}]+)\}', css).group(1)
    assert "width: 16px" in symbol_rule and "height: 16px" in symbol_rule
    assert "background: currentColor" in symbol_rule
    provenance = json.loads((STATIC / "icons/fluent-icons.json").read_text(encoding="utf-8"))
    assert provenance["source"] == "Microsoft Fluent UI System Icons" and provenance["license"] == "MIT"
    upstream = "https://raw.githubusercontent.com/microsoft/fluentui-system-icons/main/assets/"
    expected_assets = {
        "fluent-copy-20-regular.svg": upstream + "Copy/SVG/ic_fluent_copy_20_regular.svg",
        "fluent-clipboard-paste-20-regular.svg": upstream + "Clipboard%20Paste/SVG/ic_fluent_clipboard_paste_20_regular.svg",
        "fluent-checkmark-20-regular.svg": upstream + "Checkmark/SVG/ic_fluent_checkmark_20_regular.svg",
    }
    assert provenance["assets"] == expected_assets
    assert ".lut-copy.is-copied .lut-copy-symbol" in css
    for filename in expected_assets:
        assert f'url("icons/{filename}")' in css
        svg = ElementTree.parse(STATIC / "icons" / filename).getroot()
        assert svg.attrib["viewBox"] == "0 0 20 20"
        assert svg.attrib["width"] == svg.attrib["height"] == "20"
        assert svg.find("{http://www.w3.org/2000/svg}path").attrib["d"]
    license_text = (STATIC / "icons" / provenance["license_file"]).read_text(encoding="utf-8")
    assert "MIT License" in license_text and "Copyright (c) 2020 Microsoft Corporation" in license_text
    assert "lut-clipboard.png" not in css and "lut-paintbrush.png" not in css
