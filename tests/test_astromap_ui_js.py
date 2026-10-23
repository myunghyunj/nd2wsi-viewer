"""Astromap discovery and coordinate behavior without decoding image pixels."""

import json
import re
import shutil
import subprocess
from pathlib import Path
from xml.etree import ElementTree

import pytest


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "nd2wsi" / "static"
MODULE = STATIC / "astromap-v1.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _run(script):
    result = subprocess.run(
        [NODE, "-e", script, str(MODULE)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


GEOMETRY_SCRIPT = r"""
const api = require(process.argv[1]);
const out = {};
const input = {x:10.8, y:20.2, w:30.4, h:40.2};
out.rect = api.normalizeRect(input, 100, 100);
out.clipped = api.normalizeRect({x:-10, y:95, w:30, h:20}, 100, 100);
out.invalid = [
  null, {}, {x:1,y:1,w:0,h:10}, {x:1,y:1,w:-1,h:2},
  {x:NaN,y:1,w:2,h:2}, {x:1,y:Infinity,w:2,h:2},
  {x:100,y:0,w:10,h:10}, {x:0,y:101,w:10,h:10},
].map(r => api.normalizeRect(r, 100, 100));
out.inputAfter = input;
out.touching = api.overlaps({x:0,y:0,w:10,h:10}, {x:10,y:0,w:10,h:10});
out.intersection = api.overlaps({x:0,y:0,w:10,h:10}, {x:9,y:9,w:10,h:10});
out.containment = api.overlaps({x:0,y:0,w:10,h:10}, {x:2,y:2,w:3,h:3});
out.sizes = [[40000,20000], [500,900], [8001,4001], [100000,1]].map(([w,h]) => api.exportSize(w,h));
const annotations = [
  {id:'ordinary',type:'box',x:1,y:1,w:2,h:2,text:'manual'},
  {id:'one',type:'box',astromap:true,x:10,y:20,w:30,h:40,text:''},
  {id:'hidden',type:'box',astromap:true,astromapEnabled:false,x:80,y:90,w:5,h:6,text:'hidden'},
  {id:'three',type:'box',astromap:true,x:100,y:110,w:120,h:130,text:''},
  {id:'note',type:'box',astromap:true,x:300,y:400,w:500,h:600,text:'capsule'},
  {id:'pin',type:'pin',astromap:true,x:10,y:20,text:'pin'},
];
const before = JSON.stringify(annotations);
out.selected = api.selectedRois(annotations);
out.annotationUnchanged = JSON.stringify(annotations) === before;
out.repeated = JSON.stringify(api.selectedRois(annotations)) === JSON.stringify(out.selected);
process.stdout.write(JSON.stringify(out));
"""


def test_roi_normalization_clips_to_source_and_rejects_empty_invalid_geometry():
    out = _run(GEOMETRY_SCRIPT)
    assert out["rect"] == {"x": 10, "y": 20, "w": 32, "h": 41}
    assert out["clipped"] == {"x": 0, "y": 95, "w": 20, "h": 5}
    assert out["invalid"] == [None] * 8
    assert out["inputAfter"] == {"x": 10.8, "y": 20.2, "w": 30.4, "h": 40.2}
    assert out["touching"] is False
    assert out["intersection"] is True
    assert out["containment"] is True


def test_export_keeps_source_annotations_and_omits_unselected_boxes():
    out = _run(GEOMETRY_SCRIPT)
    assert out["selected"] == [
        {"x": 10, "y": 20, "w": 30, "h": 40, "note": "1", "enabled": True},
        {"x": 100, "y": 110, "w": 120, "h": 130, "note": "3", "enabled": True},
        {"x": 300, "y": 400, "w": 500, "h": 600, "note": "capsule", "enabled": True},
    ]
    assert out["annotationUnchanged"] is True
    assert out["repeated"] is True
    assert out["sizes"] == [
        {"width": 8000, "height": 4000},
        {"width": 500, "height": 900},
        {"width": 8000, "height": 4000},
        {"width": 8000, "height": 1},
    ]


def test_scaled_coordinates_are_jpg_pixels_with_inclusive_right_bottom():
    out = _run(r"""
const {scaledCoordinates} = require(process.argv[1]);
const rect = {x:10000,y:5000,w:100,h:50};
const first = scaledCoordinates(rect,40000,20000,8000,4000);
const second = scaledCoordinates(rect,40000,20000,8000,4000);
process.stdout.write(JSON.stringify({
  first, second, rect,
  lastPixel: scaledCoordinates({x:40000,y:20002,w:1,h:1},40001,20003,8000,4000),
  noShrink: scaledCoordinates({x:5,y:8,w:10,h:20},100,100,100,100),
  tiny: scaledCoordinates({x:100,y:100,w:1,h:1},40000,40000,8000,8000),
}));
""")
    assert out["first"] == {"x1": 2000, "y1": 1000, "x2": 2019, "y2": 1009}
    assert out["second"] == out["first"]
    assert out["rect"] == {"x": 10000, "y": 5000, "w": 100, "h": 50}
    assert out["lastPixel"] == {"x1": 7999, "y1": 3999, "x2": 7999, "y2": 3999}
    assert out["noShrink"] == {"x1": 5, "y1": 8, "x2": 14, "y2": 27}
    assert out["tiny"] == {"x1": 20, "y1": 20, "x2": 20, "y2": 20}


def test_screen_drag_uses_source_bounds_under_rotation_reflection_and_clipping():
    out = _run(r"""
const fs = require('fs'), vm = require('vm'), path = require('path');
const app = fs.readFileSync(path.join(path.dirname(process.argv[1]), 'app.js'), 'utf8');
const start = app.indexOf('function imageBoundsOfScreenRect(');
const end = app.indexOf('function positionRubber(', start);
if (start < 0 || end < 0) throw new Error('Production coordinate conversion not found');
const context = {
  state: {info: {width:200, height:200}},
  clamp: (value, low, high) => Math.max(low, Math.min(high, value)),
  transform: p => p,
  viewerElementToImagePoint(p) { return context.transform(p); },
};
vm.createContext(context);
vm.runInContext(app.slice(start, end) + '\nthis.bounds = imageBoundsOfScreenRect;', context);
const out = {};
out.identity = context.bounds({x:10,y:20}, {x:40,y:60});
context.transform = p => ({x:p.y, y:200-p.x});
out.quarterTurn = context.bounds({x:140,y:10}, {x:180,y:40});
context.transform = p => ({x:200-p.x, y:p.y});
out.mirror = context.bounds({x:160,y:20}, {x:190,y:60});
context.transform = p => ({x:0.6*p.x+0.8*p.y, y:-0.8*p.x+0.6*p.y+200});
out.oblique = context.bounds({x:100,y:100}, {x:120,y:110});
context.transform = p => ({x:p.x + 1e-10, y:p.y - 1e-10});
out.roundoff = context.bounds({x:10,y:20}, {x:40,y:60});
context.transform = p => p;
out.clipped = context.bounds({x:-10,y:190}, {x:10,y:220});
process.stdout.write(JSON.stringify(out));
""")
    expected = {"x": 10, "y": 20, "w": 30, "h": 40}
    assert out["identity"] == expected
    assert out["quarterTurn"] == expected
    assert out["mirror"] == expected
    assert out["roundoff"] == expected
    assert out["oblique"] == {"x": 140, "y": 164, "w": 20, "h": 22}
    assert out["clipped"] == {"x": 0, "y": 190, "w": 10, "h": 10}


def test_hidden_toolbar_button_precedes_shading_and_uses_existing_fluent_family():
    shell = (STATIC / "shell.html").read_text(encoding="utf-8")
    pane = (STATIC / "index.html").read_text(encoding="utf-8")
    button = re.search(r'<button\b[^>]*id="astromap-toggle"[^>]*>.*?</button>', shell, re.S)
    assert button is not None
    assert re.search(r"\bhidden\b", button.group())
    assert re.search(r"\bdisabled\b", button.group())
    # This is adjacency in the toolbar, not just document order elsewhere.
    assert re.match(r'\s*<div id="shading-control">', shell[button.end():])
    assert 'class="astromap-icon"' in button.group()
    assert re.sub(r"<[^>]*>", "", button.group()).strip() == ""
    assert 'aria-label="Astromap"' in button.group()
    assert not any(symbol in button.group() for symbol in ("⭐", "🌟", "★", "☆"))
    assert "astromap-v1.js" not in shell
    assert 'aria-keyshortcuts="Meta+Shift+A"' in button.group()
    assert pane.index("astromap-v1.js") < pane.index("app.js")

    provenance = json.loads((STATIC / "icons/fluent-icons.json").read_text(encoding="utf-8"))
    name = "fluent-star-20-regular.svg"
    assert provenance["source"] == "Microsoft Fluent UI System Icons"
    assert provenance["style"] == "Regular" and provenance["size_px"] == 20
    assert provenance["license"] == "MIT"
    assert provenance["assets"][name] == (
        "https://raw.githubusercontent.com/microsoft/fluentui-system-icons/main/"
        "assets/Star/SVG/ic_fluent_star_20_regular.svg"
    )
    svg = ElementTree.parse(STATIC / "icons" / name).getroot()
    assert svg.attrib["viewBox"] == "0 0 20 20"
    assert svg.find("{http://www.w3.org/2000/svg}path").attrib["d"]
    for stylesheet in ("native-shell-v1.css", "style.css"):
        css = (STATIC / stylesheet).read_text(encoding="utf-8")
        rule = re.search(r"\.astromap-icon\s*\{([^}]+)\}", css).group(1)
        assert "background:currentColor" in rule
        assert f'url("icons/{name}")' in rule
    assert "#astromap-toggle[hidden] { display:none; }" in (STATIC / "native-shell-v1.css").read_text(encoding="utf-8")


EXPORT_SCRIPT = r"""
const fs = require('fs'), vm = require('vm'), path = require('path');
const api = require(process.argv[1]);
const app = fs.readFileSync(path.join(path.dirname(process.argv[1]), 'app.js'), 'utf8');
function production(name, async = false) {
  const start = app.indexOf((async ? 'async ' : '') + 'function ' + name + '(');
  if (start < 0) throw new Error(name + ' not found');
  return app.slice(start, app.indexOf('\n}', start) + 2);
}
function fixture() {
  const requests = [], downloads = [], notices = [], nodes = new Map();
  const c = vm.createContext({
    state: {
      info: {generation:'generation-a'}, astromap:{exporting:false}, channels: new Set([0,2]),
      annotations: [
        {type:'box',astromap:true,x:100,y:200,w:300,h:400,text:'keep'},
        {type:'box',astromap:true,astromapEnabled:false,x:900,y:900,w:50,h:50,text:'omit'},
        {type:'box',x:1,y:2,w:3,h:4,text:'manual'},
      ],
    },
    context: {key:'slide-a:generation-a:0,1,2',frame:{t:0,p:1,z:2}},
    ownerReady:true, imageReady:true, locked:false,
    activeFrameContext: () => c.context,
    annotationOwnerReady: () => c.ownerReady,
    spatialImageReady: () => c.imageReady,
    annLocked: () => c.locked,
    closeEditor() {}, refreshAstromapPanel() {},
    showToast: value => notices.push(value),
    fmtInt: value => String(value), lutParam: () => '0,100,1;10,500,1.2',
    window: {Nd2Astromap:api}, URL,
    location: {origin:'http://127.0.0.1:8000',pathname:'/s/a/',href:'http://127.0.0.1:8000/s/a/'},
    document: {
      createElement(tag) {
        if (tag !== 'a') throw new Error('Unexpected raster/DOM allocation: ' + tag);
        return {click() { downloads.push({href:this.href,download:this.download}); }, remove() {}};
      },
      body: {append() {}},
    },
    $(id) { if (!nodes.has(id)) nodes.set(id, {}); return nodes.get(id); },
    fetch: (url, options) => new Promise(resolve => requests.push({url, options, resolve})),
  });
  vm.runInContext(production('astromapReady') + '\n' + production('exportAstromap',true), c);
  const response = (extra = {}, ok = true) => requests.at(-1).resolve({ok,status:ok?200:409,
    json: async () => ({download_url:'api/astromap/download?token=export-token',filename:'slide_astromap.zip',
      count:1,width:8000,height:4000,...extra})});
  return {c,requests,downloads,notices,response,nodes};
}
"""


def _run_export(body):
    return _run(EXPORT_SCRIPT + "\n(async () => {\n" + body + "\n})().then(result => process.stdout.write(JSON.stringify(result)));\n")


def test_export_posts_source_coordinates_once_and_downloads_without_browser_image_buffers():
    out = _run_export(r"""
const f = fixture(), before = JSON.stringify(f.c.state.annotations);
const first = f.c.exportAstromap();
const busy = f.c.state.astromap.exporting;
const second = await f.c.exportAstromap();
const countWhileBusy = f.requests.length;
f.response();
const completed = await first;
const repeated = f.c.exportAstromap();
f.response();
await repeated;
return {busy,second,countWhileBusy,completed,
  payloads:f.requests.map(r=>JSON.parse(r.options.body)),
  downloads:f.downloads,unchanged:before===JSON.stringify(f.c.state.annotations),
  busyAfter:f.c.state.astromap.exporting};
""")
    assert out["busy"] is True and out["busyAfter"] is False
    assert out["second"] is False and out["countWhileBusy"] == 1
    assert out["completed"] is True and out["unchanged"] is True
    payload = {
        "generation": "generation-a",
        "rois": [{"x": 100, "y": 200, "w": 300, "h": 400, "note": "keep", "enabled": True}],
        "channels": [0, 2], "window": "0,100,1;10,500,1.2", "frame": {"t": 0, "p": 1, "z": 2},
    }
    assert out["payloads"] == [payload, payload]
    assert out["downloads"] == [{
        "href": "http://127.0.0.1:8000/s/a/api/astromap/download?token=export-token",
        "download": "slide_astromap.zip",
    }] * 2


def test_export_rejects_unowned_frames_empty_selection_and_stale_async_results():
    out = _run_export(r"""
const blocked = [];
for (const update of [
  f => f.c.ownerReady=false,
  f => f.c.imageReady=false,
  f => f.c.locked=true,
  f => f.c.context=null,
  f => f.c.state.quitPreparing=true,
  f => f.c.state.annotations=[],
]) {
  const f = fixture(); update(f);
  blocked.push({result:await f.c.exportAstromap(),requests:f.requests.length});
}
const f = fixture(), pending = f.c.exportAstromap();
f.c.context = {...f.c.context,key:'slide-a:generation-a:0,2,2'};
f.response();
const stale = await pending;
const g = fixture(), unsafe = g.c.exportAstromap();
g.response({download_url:'https://untrusted.example/image.zip'});
const rejectedLocation = await unsafe;
return {blocked,stale,downloads:f.downloads,busy:f.c.state.astromap.exporting,
  message:f.nodes.get('astromap-status').textContent,rejectedLocation,
  unsafeDownloads:g.downloads};
""")
    assert out["blocked"] == [{"result": False, "requests": 0}] * 6
    assert out["stale"] is False and out["downloads"] == [] and out["busy"] is False
    assert "displayed image changed" in out["message"]
    assert out["rejectedLocation"] is False and out["unsafeDownloads"] == []


def test_pane_toggle_requires_trusted_parent_and_matching_slide_context():
    out = _run(r"""
const fs = require('fs'), vm = require('vm'), path = require('path');
const app = fs.readFileSync(path.join(path.dirname(process.argv[1]), 'app.js'), 'utf8');
const start = app.indexOf('function wireCompareRelay(');
const end = app.indexOf('  for (const kind of ["pointerdown", "keydown", "beforeinput"])',start);
if (start < 0 || end < 0) throw new Error('Production message relay not found');
const parent = {}, listeners = {}, toggles = [], checked = [];
const c = vm.createContext({
  window: {parent,addEventListener(kind,fn) { listeners[kind] = fn; }},
  location:{origin:'http://localhost:8000'}, VIEWPORT_PROTOCOL_VERSION:2,
  state:{viewportRelay:{commandGate:{matchesLocal(data) {
    checked.push(data.paneInstanceId); return data.paneInstanceId === 'pane-current' && data.contextKey === 'site-current';
  }}}},
  refreshSpatialContext() {}, currentSlideSid:()=> 'slide-a',
  toggleAstromap:forceOpen=>toggles.push(forceOpen),
});
vm.runInContext(app.slice(start,end) + '\n}',c);
c.wireCompareRelay();
const data = {nd2wsi:'astromap-toggle',version:2,sid:'slide-a',paneInstanceId:'pane-current',contextKey:'site-current',open:true};
const event = {source:parent,origin:'http://localhost:8000',data};
for (const invalid of [
  {...event,source:{}}, {...event,origin:'http://untrusted.example'},
  {...event,data:{...data,version:1}}, {...event,data:{...data,sid:'slide-b'}},
  {...event,data:{...data,paneInstanceId:'pane-old'}},
  {...event,data:{...data,contextKey:'site-old'}},
]) listeners.message(invalid);
const before = toggles.length;
listeners.message(event);
listeners.message({...event,data:{...data,open:false}});
process.stdout.write(JSON.stringify({before,toggles,checked}));
""")
    assert out == {"before": 0, "toggles": [True, False], "checked": ["pane-old", "pane-current", "pane-current", "pane-current"]}


def test_astromap_draw_keeps_vector_geometry_and_excludes_native_panel_gestures():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    pane = (STATIC / "index.html").read_text(encoding="utf-8")
    tools = app[app.index("function wireTools("):app.index("function astromapReady(")]
    rubber = app[app.index("function positionRubber("):app.index("function wireTools(")]
    annotations = app[app.index("function renderAnnotations("):app.index("function renderLandmarks(")]
    assert 'state.tool === "astromap"' in tools
    assert "dragContext !== activeFrameContext()?.key" in tools
    assert "addAstromapRoi(rectangle)" in tools and "positionRubber(start, end)" in tools
    assert "imageBoundsOfScreenRect(a, b)" in rubber
    assert 'svgEl("polygon"' in rubber and "roiOverlayPoints(r)" in rubber
    assert 'a.astromap === true && a.astromapEnabled === false' in annotations
    assert 'svgEl("polygon"' in annotations
    assert not any(pixel_api in rubber + annotations for pixel_api in (
        "drawImage(", "getImageData(", "putImageData(", "createImageBitmap(", "toDataURL(",
    ))
    assert re.search(r'<section\b[^>]*class="mac-window hidden"[^>]*id="win-astromap"', pane)
    exclusions = re.search(r'const NATIVE_GESTURE_EXCLUSIONS = "([^"]+)"', app).group(1)
    assert ".mac-window" in [selector.strip() for selector in exclusions.split(",")]


SHORTCUT_SCRIPT = r"""
const path = require('path');
const Router = require(path.join(path.dirname(process.argv[1]),'shortcut-router-v1.js'));
function target(spec={}) {
  return {tagName:spec.tag||'DIV',type:spec.type,isContentEditable:!!spec.editable,
    closest(selector) {
      return spec.insideTextbox && selector.includes('role="textbox"') ? {} : null;
    },
    ownerDocument:{getSelection:()=>({isCollapsed:!spec.selection,toString:()=>spec.selection||''})},
  };
}
function event(extra={},spec={}) {
  return {key:'A',code:'KeyA',metaKey:true,ctrlKey:false,shiftKey:true,altKey:false,
    target:target(spec),...extra};
}
"""


def test_astromap_shortcut_uses_exact_platform_chord_and_korean_physical_key():
    out = _run(SHORTCUT_SCRIPT + r"""
const mac=e=>Router.isAstromapShortcut(e,'MacIntel');
const win=e=>Router.isAstromapShortcut(e,'Win32');
process.stdout.write(JSON.stringify({
  accepted:[mac(event()),win(event({metaKey:false,ctrlKey:true})),
    mac(event({key:'ㅁ'})),mac(event({repeat:true}))],
  rejected:[mac(event({metaKey:false,ctrlKey:true})),win(event()),
    mac(event({ctrlKey:true})),mac(event({metaKey:false})),
    mac(event({shiftKey:false})),mac(event({altKey:true})),
    mac(event({key:'B',code:'KeyB'}))],
  ordinaryAnnotation:Router.panelForEvent(event({shiftKey:false}),'MacIntel'),
  astromapNotOrdinaryPanel:Router.panelForEvent(event(),'MacIntel'),
}));
""")
    assert out["accepted"] == [True] * 4
    assert out["rejected"] == [False] * 7
    assert out["ordinaryAnnotation"] == "annot" and out["astromapNotOrdinaryPanel"] is None


def test_astromap_shortcut_preserves_text_controls_composition_selection_and_shadow_editors():
    out = _run(SHORTCUT_SCRIPT + r"""
const guarded = [
  event({isComposing:true}),event({keyCode:229}),event({defaultPrevented:true}),
  event({}, {tag:'INPUT',type:'text'}),event({}, {tag:'INPUT',type:'checkbox'}),
  event({}, {tag:'TEXTAREA'}),event({}, {tag:'SELECT'}),
  event({}, {editable:true}),event({}, {insideTextbox:true}),
  event({}, {selection:'selected text'}),
  event({composedPath:()=>[target({tag:'INPUT',type:'text'})]}),
];
process.stdout.write(JSON.stringify(guarded.map(e=>Router.isAstromapShortcut(e,'MacIntel'))));
""")
    assert out == [False] * 11


SHELL_SCRIPT = r"""
const fs = require('fs'), vm = require('vm');
const shell = fs.readFileSync(path.join(path.dirname(process.argv[1]),'shell-v1.js'),'utf8');
function production(source,name) {
  const start=source.indexOf('function '+name+'(');
  if(start<0)throw new Error(name+' not found');
  return source.slice(start,source.indexOf('\n}',start)+2);
}
function shellFixture(platform='MacIntel') {
  const listeners=new Map(),sent=[],nodes=new Map();
  function $(id) {
    if(!nodes.has(id))nodes.set(id,{hidden:true,disabled:true,attrs:{},classList:{toggle(){}},
      setAttribute(k,v){this.attrs[k]=v;}});
    return nodes.get(id);
  }
  const c=vm.createContext({
    $,active:'a',readyFrames:new Set(['a','b']),quitPreparation:null,pairPicker:{open:false},
    astromapStates:new Map(),astromapUnlocked:false,
    compare:{enabled:false,landmark:{active:false},states:new Map([
      ['a',{paneInstanceId:'pane-a',contextEpoch:1,spatialContext:{key:'site-a'}}],
      ['b',{paneInstanceId:'pane-b',contextEpoch:1,spatialContext:{key:'site-b'}}],
    ])},
    frames:new Map([['a',{contentWindow:{}}],['b',{contentWindow:{}}]]),
    ShortcutRouter:{...Router,isAstromapShortcut:e=>Router.isAstromapShortcut(e,platform)},
    location:{origin:'http://localhost:8000'},VIEWPORT_PROTOCOL_VERSION:2,
    postToSlide:(sid,data)=>{sent.push({sid,data});return true;},
    window:{addEventListener(kind,fn){
      if(!listeners.has(kind))listeners.set(kind,[]);listeners.get(kind).push(fn);
    }},
  });
  for(const name of ['localIdentity','sameIdentity','frameSidForSource'])
    vm.runInContext(production(shell,name),c);
  const begin=shell.indexOf('function refreshAstromapButton('),end=shell.indexOf('function showError(',begin);
  const message=shell.indexOf('window.addEventListener("message", (event) => {');
  const classification=shell.indexOf('ShortcutRouter?.isAstromapShortcut(event)');
  const keydown=shell.lastIndexOf('window.addEventListener("keydown"',classification);
  if([begin,end,message,classification,keydown].some(n=>n<0))throw new Error('Production shortcut wiring not found');
  vm.runInContext(shell.slice(begin,end),c);
  vm.runInContext(shell.slice(message,shell.indexOf('\n});',message)+4),c);
  vm.runInContext(shell.slice(keydown,shell.indexOf('\n}, true);',keydown)+10),c);
  c.refreshAstromapButton();
  function emit(kind,value){(listeners.get(kind)||[]).forEach(fn=>fn(value));}
  function press(extra={},spec={}) {
    const e=event({...extra,prevented:0,stopped:0,
      preventDefault(){this.prevented++;this.defaultPrevented=true;},stopPropagation(){this.stopped++;}},spec);
    emit('keydown',e);return {prevented:e.prevented,stopped:e.stopped};
  }
  const packet=(sid='a',extra={})=>({source:c.frames.get(sid).contentWindow,origin:c.location.origin,
    data:{nd2wsi:'astromap-shortcut',version:2,sid,paneInstanceId:'pane-'+sid,
      contextEpoch:1,spatialContextKey:'site-'+sid,...extra}});
  return {c,$,sent,emit,press,packet};
}
"""


def _run_shell(body):
    return _run(SHORTCUT_SCRIPT + SHELL_SCRIPT + "\n" + body)


def test_shortcut_reveals_icon_and_opens_active_panel_once_then_toggles_without_key_repeat():
    out = _run_shell(r"""
const f=shellFixture(),initial=f.$('astromap-toggle').hidden;
f.$('astromap-toggle').onclick();const beforeShortcut=f.sent.length;
const first=f.press(),repeat=f.press({repeat:true});
const afterRepeat=f.sent.length;
f.emit('message',f.packet('a',{nd2wsi:'astromap-state',open:true}));
const displayed={hidden:f.$('astromap-toggle').hidden,pressed:f.$('astromap-toggle').attrs['aria-pressed']};
f.press();f.c.active='b';f.c.refreshAstromapButton();f.$('astromap-toggle').onclick();
const g=shellFixture('Win32');g.press({metaKey:false,ctrlKey:true});
process.stdout.write(JSON.stringify({initial,beforeShortcut,first,repeat,afterRepeat,displayed,
  sent:f.sent,windows:g.sent}));
""")
    assert out["initial"] is True and out["beforeShortcut"] == 0
    assert out["first"] == out["repeat"] == {"prevented": 1, "stopped": 1}
    assert out["afterRepeat"] == 1 and out["displayed"] == {"hidden": False, "pressed": "true"}
    first = {"sid": "a", "data": {"nd2wsi": "astromap-toggle", "version": 2, "sid": "a", "paneInstanceId": "pane-a", "contextEpoch": 1, "spatialContextKey": "site-a", "open": True}}
    assert out["sent"] == [first,
        {"sid": "a", "data": {**first["data"], "open": False}},
        {"sid": "b", "data": {"nd2wsi": "astromap-toggle", "version": 2, "sid": "b", "paneInstanceId": "pane-b", "contextEpoch": 1, "spatialContextKey": "site-b", "open": False}},
    ]
    assert out["windows"] == [first]


def test_shortcut_relay_requires_trusted_active_pane_current_site_and_ready_session():
    out = _run_shell(r"""
const blocked=[];
for(const mutate of [
  (f,p)=>({...p,origin:'http://untrusted.example'}),
  (f,p)=>({...p,source:{}}),
  (f,p)=>({...p,data:{...p.data,version:1}}),
  (f,p)=>f.packet('b'),
  (f,p)=>({...p,data:{...p.data,sid:'b'}}),
  (f,p)=>({...p,data:{...p.data,paneInstanceId:'pane-old'}}),
  (f,p)=>({...p,data:{...p.data,contextEpoch:0}}),
  (f,p)=>({...p,data:{...p.data,spatialContextKey:'old-site'}}),
  (f,p)=>{f.c.readyFrames.delete('a');return p;},
  (f,p)=>{f.c.quitPreparation={};return p;},
  (f,p)=>{f.c.pairPicker.open=true;return p;},
]) {
  const f=shellFixture();f.emit('message',mutate(f,f.packet()));
  blocked.push({hidden:f.$('astromap-toggle').hidden,sent:f.sent.length});
}
const f=shellFixture();f.emit('message',f.packet());
const afterValid=f.sent.length;
f.c.compare.states.set('a',{paneInstanceId:'pane-new',contextEpoch:2,spatialContext:{key:'site-a'}});
f.emit('message',f.packet());
const afterReload=f.sent.length;
const g=shellFixture();g.c.compare.states.delete('a');g.press();
process.stdout.write(JSON.stringify({blocked,afterValid,afterReload,missingIdentity:g.sent.length}));
""")
    assert out["blocked"] == [{"hidden": True, "sent": 0}] * 11
    assert out["afterValid"] == out["afterReload"] == 1
    assert out["missingIdentity"] == 0


def test_pane_shortcut_relays_current_identity_once_and_standalone_force_open_does_not_toggle_closed():
    out = _run(SHORTCUT_SCRIPT + SHELL_SCRIPT + r"""
const app=fs.readFileSync(path.join(path.dirname(process.argv[1]),'app.js'),'utf8');
function paneFixture(platform='MacIntel') {
  const sent=[],listeners=new Map(),toolChanges=[];
  const c=vm.createContext({
    hidden:true,refreshes:0,
    state:{tool:null,quitPreparing:false,windows:{astromap:{
      isHidden:()=>c.hidden,open:()=>{c.hidden=false;},close:()=>{c.hidden=true;},
    }},viewportRelay:{commandGate:{spatialContext:{key:'site-a'},
      envelope:()=>({paneInstanceId:'pane-a',contextEpoch:4,spatialContextKey:'site-a'})}}},
    window:{Nd2ShortcutRouter:{...Router,isAstromapShortcut:e=>Router.isAstromapShortcut(e,platform)},
      parent:{postMessage:(data,origin)=>sent.push({data,origin})},
      addEventListener:(kind,fn)=>listeners.set(kind,fn)},
    location:{origin:'http://localhost:8000'},VIEWPORT_PROTOCOL_VERSION:2,
    currentSlideSid:()=> 'a',refreshSpatialContext:()=>{c.refreshes++;},refreshAstromapPanel(){},
    setTool:tool=>{c.state.tool=tool;toolChanges.push(tool);},
  });
  for(const name of ['spatialIdentity','requestAstromap','toggleAstromap','wireKeys'])
    vm.runInContext(production(app,name),c);
  c.wireKeys();
  function press(extra={},spec={}) {
    const e=event({...extra,prevented:0,stopped:0,
      preventDefault(){this.prevented++;this.defaultPrevented=true;},stopPropagation(){this.stopped++;}},spec);
    listeners.get('keydown')(e);return {prevented:e.prevented,stopped:e.stopped};
  }
  return {c,sent,press,toolChanges};
}
const f=paneFixture();
const typing=f.press({}, {tag:'INPUT',type:'text'}),composing=f.press({isComposing:true});
const first=f.press({key:'ㅁ'}),repeat=f.press({repeat:true});
const beforeStandalone={sent:f.sent,hidden:f.c.hidden,refreshes:f.c.refreshes};
f.c.window.parent=f.c.window;f.press();const standaloneOpened=!f.c.hidden;
f.c.toggleAstromap(true);const forcedRemainsOpen=!f.c.hidden;
f.c.state.tool='astromap';f.press();const standaloneClosed=f.c.hidden;
const g=paneFixture('Win32');g.press({metaKey:false,ctrlKey:true});
process.stdout.write(JSON.stringify({typing,composing,first,repeat,beforeStandalone,
  standaloneOpened,forcedRemainsOpen,standaloneClosed,toolChanges:f.toolChanges,windows:g.sent}));
""")
    assert out["typing"] == out["composing"] == {"prevented": 0, "stopped": 0}
    assert out["first"] == out["repeat"] == {"prevented": 1, "stopped": 1}
    packet = {"data": {"nd2wsi": "astromap-shortcut", "version": 2, "sid": "a", "paneInstanceId": "pane-a", "contextEpoch": 4, "spatialContextKey": "site-a", "spatialContext": {"key": "site-a"}}, "origin": "http://localhost:8000"}
    assert out["beforeStandalone"] == {"sent": [packet], "hidden": True, "refreshes": 1}
    assert out["windows"] == [packet]
    assert out["standaloneOpened"] and out["forcedRemainsOpen"] and out["standaloneClosed"]
    assert out["toolChanges"] == [None]


DRAG_SCRIPT = r"""
const fs = require('fs'), vm = require('vm'), path = require('path');
const app = fs.readFileSync(path.join(path.dirname(process.argv[1]), 'app.js'), 'utf8');
function production(name) {
  const start = app.indexOf('function ' + name + '(');
  if (start < 0) throw new Error(name + ' not found');
  return app.slice(start, app.indexOf('\n}', start) + 2);
}
function fixture() {
  const nodes = new Map(), navigation = [], added = [];
  function $(id) {
    if (!nodes.has(id)) nodes.set(id, {style:{},handlers:{},classList:{toggle(){},add(){},remove(){}},
      addEventListener(kind,fn) { this.handlers[kind]=fn; },setPointerCapture(){}});
    return nodes.get(id);
  }
  const c = vm.createContext({
    $, hidden:false,ready:true,
    state:{info:{width:1000,height:1000},tool:null,landmark:{active:false},
      viewer:{setMouseNavEnabled:enabled=>navigation.push(enabled)},
      windows:{astromap:{isHidden:()=>c.hidden}}},
    astromapReady:()=>c.ready,annotationOwnerReady:()=>true,annLocked:()=>false,
    activeFrameContext:()=>({key:'slide-a:generation-a'}),
    elementPoint:event=>({x:event.x,y:event.y}),imgPoint:point=>point,
    positionRubber:(a,b)=>({x:Math.min(a.x,b.x),y:Math.min(a.y,b.y),w:Math.abs(b.x-a.x),h:Math.abs(b.y-a.y)}),
    addAstromapRoi:rect=>added.push(rect),
    renderAnnotations(){},clearRoi(){},wireRoiDims(){},wireAnnotationPanel(){},
  });
  const start = app.indexOf('const TOOL_BTN = {');
  vm.runInContext(app.slice(start, app.indexOf('\n};',start)+3),c);
  vm.runInContext(production('setTool') + '\n' + production('wireTools'),c);
  c.wireTools();
  function emit(kind,overrides={}) {
    const event={button:0,shiftKey:true,metaKey:false,ctrlKey:false,altKey:false,
      x:10,y:20,pointerId:1,prevented:0,stopped:0,
      preventDefault(){this.prevented++;},stopPropagation(){this.stopped++;},...overrides};
    $('stage').handlers[kind](event);
    return {prevented:event.prevented,stopped:event.stopped};
  }
  return {c,$,navigation,added,emit};
}
"""


def test_shift_primary_drag_enters_astromap_only_with_open_ready_panel_and_no_other_modifier():
    out = _run(DRAG_SCRIPT + r"""
const blocked=[];
for (const {event={},update=()=>{}} of [
  {event:{button:2}}, {event:{shiftKey:false}},
  {event:{metaKey:true}}, {event:{ctrlKey:true}}, {event:{altKey:true}},
  {update:f=>f.c.hidden=true}, {update:f=>f.c.ready=false},
]) {
  const f=fixture();update(f);
  const dispatched=f.emit('pointerdown',event);
  blocked.push({tool:f.c.state.tool,navigation:f.navigation,...dispatched});
}
const f=fixture(),dispatched=f.emit('pointerdown');
process.stdout.write(JSON.stringify({blocked,valid:{tool:f.c.state.tool,navigation:f.navigation,...dispatched}}));
""")
    assert out["blocked"] == [{"tool": None, "navigation": [], "prevented": 0, "stopped": 0}] * 7
    assert out["valid"] == {"tool": "astromap", "navigation": [False], "prevented": 1, "stopped": 1}


def test_completed_shift_drag_adds_box_then_restores_normal_navigation_after_release():
    out = _run(DRAG_SCRIPT + r"""
const f=fixture();
f.emit('pointerdown');
f.emit('pointermove',{x:80,y:110});
f.emit('pointerup',{x:80,y:110,shiftKey:false});
const plainDown=f.emit('pointerdown',{x:100,y:120,shiftKey:false});
const plainMove=f.emit('pointermove',{x:130,y:160,shiftKey:false});
const plainUp=f.emit('pointerup',{x:130,y:160,shiftKey:false});
process.stdout.write(JSON.stringify({added:f.added,tool:f.c.state.tool,navigation:f.navigation,
  rubber:f.$('rubber').style.display,plainDown,plainMove,plainUp}));
""")
    assert out["added"] == [{"x": 10, "y": 20, "w": 70, "h": 90}]
    assert out["tool"] is None and out["navigation"] == [False, True]
    assert out["rubber"] == "none"
    assert out["plainDown"] == out["plainMove"] == out["plainUp"] == {"prevented": 0, "stopped": 0}
