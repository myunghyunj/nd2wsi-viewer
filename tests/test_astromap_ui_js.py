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
        encoding="utf-8",
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


def test_visible_toolbar_button_precedes_shading_and_uses_existing_fluent_family():
    shell = (STATIC / "shell.html").read_text(encoding="utf-8")
    pane = (STATIC / "index.html").read_text(encoding="utf-8")
    button = re.search(r'<button\b[^>]*id="astromap-toggle"[^>]*>.*?</button>', shell, re.S)
    assert button is not None
    opening_tag = button.group().split(">", 1)[0]
    assert not re.search(r"\bhidden\b", opening_tag)
    assert re.search(r"\bdisabled\b", opening_tag)
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
    closeEditor() {}, refreshAstromapPanel() {}, refreshAstromapImport() {}, wireAstromapImport() {},
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
  vm.runInContext([production('astromapReady'),production('astromapDisplayNote'),
    production('clearAstromapRois'),production('exportAstromap',true)].join('\n'), c);
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


def test_pdf_button_is_below_existing_zip_action_and_keeps_its_dark_yellow_color_in_both_themes():
    pane = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert re.search(
        r'id="astromap-export"[^>]*>Export JPG \+ TXT</button>\s*</div>\s*'
        r'<div class="tool-row footer astromap-pdf-footer">\s*'
        r'<button[^>]*id="astromap-export-pdf"[^>]*>Export ZIP with PDF</button>', pane,
    )
    button = re.search(r'<button[^>]*id="astromap-export-pdf"[^>]*>', pane).group()
    assert 'class="btn astromap-pdf full"' in button and "SG-layout ROI navigation PDF" in button
    assert ".btn.astromap-pdf, :root.light .btn.astromap-pdf" in css


def test_export_buttons_send_pdf_only_for_explicit_pdf_action_and_report_contents():
    out = _run_export(r"""
const f=fixture();
f.c.MutationObserver=class {observe(){} disconnect(){}};
f.c.window.addEventListener=()=>{};
vm.runInContext(production('wireAstromap'), f.c);
f.c.wireAstromap();
const ordinary=f.nodes.get('astromap-export').onclick({type:'click'});
const plainStatus=f.nodes.get('astromap-status').textContent;
f.response();await ordinary;
const pdf=f.nodes.get('astromap-export-pdf').onclick({type:'click'});
const pdfStatus=f.nodes.get('astromap-status').textContent;
f.response();await pdf;
const success=f.nodes.get('astromap-status').textContent;
const eventAsArgument=f.c.exportAstromap({type:'click'});
f.response();await eventAsArgument;
return {payloads:f.requests.map(r=>JSON.parse(r.options.body)),plainStatus,pdfStatus,success};
""")
    assert "include_pdf" not in out["payloads"][0]
    assert out["payloads"][1] == {**out["payloads"][0], "include_pdf": True}
    assert out["payloads"][2] == out["payloads"][0]
    assert "PDF" not in out["plainStatus"]
    assert "PDF" in out["pdfStatus"] and "JPG + TXT + PDF in ZIP" in out["success"]


@pytest.mark.parametrize("include_pdf", [False, True])
def test_export_modes_share_busy_guard_and_both_buttons_restore_after_failure(include_pdf):
    out = _run_export(f"const includePdf={json.dumps(include_pdf)};" + r"""
const f=fixture();
f.c.state.astromap.rows=new Map();
Object.assign(f.c.state.info,{width:8000,height:4000});
f.c.astromapItems=()=>[];f.c.frameOwnsRoi=()=>true;f.c.postAstromapState=()=>{};
vm.runInContext(production('refreshAstromapPanel'),f.c);
const disabled=()=>['astromap-export','astromap-export-pdf'].map(id=>!!f.nodes.get(id).disabled);
f.c.refreshAstromapPanel();const initial=disabled();
const pending=f.c.exportAstromap(includePdf),during=disabled();
const duplicate=await f.c.exportAstromap(!includePdf),count=f.requests.length;
f.response({error:'Failed to write export'},false);
const result=await pending,after=disabled();
const labels=['astromap-export','astromap-export-pdf'].map(id=>f.nodes.get(id).textContent);
f.c.state.annotations=[];f.c.refreshAstromapPanel();const empty=disabled();
return {initial,during,duplicate,count,result,after,labels,empty};
""")
    assert out["initial"] == out["after"] == [False, False]
    assert out["during"] == out["empty"] == [True, True]
    assert out["duplicate"] is False and out["count"] == 1 and out["result"] is False
    assert out["labels"] == ["Export JPG + TXT", "Export ZIP with PDF"]


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


IMPORT_SCRIPT = EXPORT_SCRIPT + r"""
function importFixture() {
  const f=fixture(), c=f.c;
  c.window.Nd2AstromapImport=require(path.join(path.dirname(process.argv[1]),'astromap-import-v1.js'));
  Object.assign(c.state.info,{width:1000,height:500});
  c.astromapItems=()=>c.state.annotations.filter(a=>a.type==='box'&&a.astromap);
  c.saved=0;c.annotationsChanged=()=>{c.saved++;c.refreshAstromapImport();};
  vm.runInContext('let annCounter=0;\n'+[
    production('refreshAstromapImport'),production('readAstromapImportFile',true),
    production('readAstromapImportJpg',true),production('commitAstromapImport'),production('wireAstromapImport')
  ].join('\n'),c);
  return f;
}
const importText='INDEX\tX1\tY1\tX2\tY2\tNOTE\n1\t10\t20\t29\t39\tcapsule\n2\t15\t25\t24\t34\toverlap\n';
"""


def _run_import(body):
    return _run(IMPORT_SCRIPT + "\n(async () => {\n" + body + "\n})().then(result => process.stdout.write(JSON.stringify(result)));\n")


def test_import_is_first_action_and_requires_explicit_dimensions_confirmation_before_atomic_append():
    pane = (STATIC / "index.html").read_text(encoding="utf-8")
    panel = pane[pane.index('id="win-astromap"'):pane.index('id="win-info"')]
    assert panel.index('id="astromap-import"') < panel.index('id="astromap-draw"')
    assert 'id="astromap-import-options" hidden' in panel
    assert pane.index('astromap-import-v1.js') < pane.index('app.js')
    out = _run_import(r"""
const f=importFixture(), before=JSON.stringify(f.c.state.annotations);
const read=await f.c.readAstromapImportFile({name:'regions.txt',size:importText.length,text:async()=>importText});
const pending={unchanged:before===JSON.stringify(f.c.state.annotations),saved:f.c.saved,
  shown:!f.nodes.get('astromap-import-options').hidden,width:f.nodes.get('astromap-import-width').value};
f.nodes.get('astromap-import-width').value='100';f.nodes.get('astromap-import-height').value='50';
const imported=f.c.commitAstromapImport(),again=f.c.commitAstromapImport();
const added=f.c.state.annotations.slice(3),active=f.c.state.astromap.activeId;
return {read,pending,imported,again,added:added.map(({id,...a})=>a),active:active===added[0].id,
  saved:f.c.saved,prior:JSON.stringify(f.c.state.annotations.slice(0,3))===before,
  hidden:f.nodes.get('astromap-import-options').hidden};
""")
    assert out["read"] and out["pending"] == {"unchanged": True, "saved": 0, "shown": True, "width": 1000}
    assert out["imported"] and not out["again"] and out["saved"] == 1
    assert out["prior"] and out["active"] and out["hidden"]
    assert out["added"] == [
        {"type": "box", "x": 100, "y": 200, "w": 200, "h": 200, "text": "capsule",
         "astromap": True, "astromapEnabled": True, "astromapImportIndex": 1},
        {"type": "box", "x": 150, "y": 250, "w": 100, "h": 100, "text": "overlap",
         "astromap": True, "astromapEnabled": True, "astromapImportIndex": 2},
    ]


def test_import_rejects_invalid_dimensions_and_stale_image_without_partial_mutation():
    out = _run_import(r"""
const f=importFixture(),before=JSON.stringify(f.c.state.annotations);
await f.c.readAstromapImportFile({name:'regions.txt',size:importText.length,text:async()=>importText});
f.nodes.get('astromap-import-width').value='10';f.nodes.get('astromap-import-height').value='5';
const invalid=f.c.commitAstromapImport();
f.nodes.get('astromap-import-width').value='100';f.nodes.get('astromap-import-height').value='50';
f.c.context={...f.c.context,key:'different-image'};
const stale=f.c.commitAstromapImport();
const g=importFixture();let resolve;
const loading=g.c.readAstromapImportFile({name:'late.txt',size:100,text:()=>new Promise(r=>resolve=r)});
g.c.context={...g.c.context,key:'different-image'};resolve(importText);
const late=await loading;
return {invalid,stale,late,unchanged:before===JSON.stringify(f.c.state.annotations),
  saves:f.c.saved,latePending:g.c.state.astromap.pendingImport,
  busy:g.c.state.astromap.importReading,error:g.nodes.get('astromap-import-status').textContent};
""")
    assert not any(out[k] for k in ("invalid", "stale", "late"))
    assert out["unchanged"] and out["saves"] == 0 and out["latePending"] is None
    assert not out["busy"] and "image changed" in out["error"]


def test_import_reports_empty_roi_and_grid_mismatch_without_changing_existing_annotations():
    out = _run_import(r"""
const f=importFixture(),before=JSON.stringify(f.c.state.annotations);
Object.assign(f.c.state.info,{raster:true,width:8000,height:5830});
const text='INDEX\tX1\tY1\tX2\tY2\tNOTE\n'+Array.from({length:200},(_,i)=>{
  const index=i+1;
  if(index===199)return '199\t4877\t8248\t4876\t8247\t199';
  const y=index>=58&&index<=182?5900:100;
  return `${index}\t100\t${y}\t110\t${y+10}\t${index}`;
}).join('\n');
const read=await f.c.readAstromapImportFile({name:'regions.txt',size:text.length,text:async()=>text});
return {read,message:f.nodes.get('astromap-import-status').textContent,
  unchanged:before===JSON.stringify(f.c.state.annotations),saved:f.c.saved,pending:f.c.state.astromap.pendingImport};
""")
    assert out["read"] is False and out["pending"] is None
    assert out["unchanged"] and out["saved"] == 0
    assert "INDEX 199 has zero area (4877, 8248) → (4876, 8247)" in out["message"]
    assert "125 regions are outside" in out["message"] and "first INDEX 58" in out["message"]


def test_valid_out_of_default_grid_can_still_confirm_explicit_coordinate_dimensions():
    out = _run_import(r"""
const f=importFixture(),before=JSON.stringify(f.c.state.annotations);
Object.assign(f.c.state.info,{raster:true,width:1000,height:500});
const text='INDEX\tX1\tY1\tX2\tY2\tNOTE\n42\t1200\t600\t1219\t619\tmanual grid';
const read=await f.c.readAstromapImportFile({name:'larger-grid.txt',size:text.length,text:async()=>text});
const unchanged=before===JSON.stringify(f.c.state.annotations);
f.nodes.get('astromap-import-width').value='2000';f.nodes.get('astromap-import-height').value='1000';
const imported=f.c.commitAstromapImport();
return {read,unchanged,imported,last:f.c.state.annotations.at(-1),saved:f.c.saved};
""")
    assert out["read"] and out["unchanged"] and out["imported"] and out["saved"] == 1
    assert {key: out["last"][key] for key in ("x", "y", "w", "h", "astromapImportIndex")} == {
        "x": 600, "y": 300, "w": 10, "h": 10, "astromapImportIndex": 42,
    }


@pytest.mark.parametrize("image_bytes", [
    [255, 216, 255, 192, 0, 11, 8, 0, 50, 0, 100, 1, 1, 17, 0, 255, 217],
    [137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13, 73, 72, 68, 82,
     0, 0, 0, 100, 0, 0, 0, 50, 8, 2, 0, 0, 0, 0, 0, 0, 0],
], ids=["jpeg", "png"])
def test_matching_image_only_changes_visible_dimensions_until_import_is_confirmed(image_bytes):
    pane = (STATIC / "index.html").read_text(encoding="utf-8")
    assert '>Choose matching small.jpg</button>' in pane
    assert 'accept=".jpg,.jpeg,.png,image/jpeg,image/png"' in pane
    out = _run_import(r"""
const f=importFixture(),before=JSON.stringify(f.c.state.annotations);
f.c.state.info={...f.c.state.info,raster:true,width:9000,height:4500};
await f.c.readAstromapImportFile({name:'regions.txt',size:importText.length,text:async()=>importText});
const rasterDefault=[f.nodes.get('astromap-import-width').value,f.nodes.get('astromap-import-height').value];
const bytes=new Uint8Array(IMAGE_BYTES);
const blob=new Blob([bytes]);blob.name='matching-image';
const read=await f.c.readAstromapImportJpg(blob);
return {read,rasterDefault,size:[f.nodes.get('astromap-import-width').value,f.nodes.get('astromap-import-height').value],
  saved:f.c.saved,unchanged:before===JSON.stringify(f.c.state.annotations)};
""".replace("IMAGE_BYTES", json.dumps(image_bytes)))
    assert out == {"read": True, "rasterDefault": [9000, 4500], "size": [100, 50], "saved": 0, "unchanged": True}


def test_overlay_label_chip_selects_astromap_roi_before_opening_editor():
    out = _run_export(r"""
const f=fixture(),c=f.c,events=[];
c.state.annotations=[{id:'two',type:'box',astromap:true},{id:'normal',type:'box'}];
c.svgEl=()=>({insertBefore(){},addEventListener(kind,handler){this[kind]=handler;}});
c.selectAstromapRoi=(id,locate)=>events.push(['select',id,locate]);
c.openEditor=id=>events.push(['editor',id]);
vm.runInContext(production('chip'),c);
const event={stopPropagation(){events.push(['stop']);}};
c.chip({},0,0,'2: note','two').pointerdown(event);
c.chip({},0,0,'ordinary','normal').pointerdown(event);
return events;
""")
    assert out == [["stop"], ["select", "two", False], ["editor", "two"],
                   ["stop"], ["editor", "normal"]]


def test_numbered_roi_selection_locates_one_box_colors_others_grey_and_deletion_keeps_nearest():
    out = _run_export(r"""
const f=fixture(),c=f.c,polygons=[],located=[];
c.state.astromap.activeId='one';c.state.viewer={};
c.state.annotations=[{id:'one',type:'box',astromap:true,x:0,y:0,w:10,h:10},
  {id:'two',type:'box',astromap:true,x:20,y:20,w:10,h:10},
  {id:'three',type:'box',astromap:true,x:40,y:40,w:10,h:10},
  {id:'normal',type:'box',x:60,y:60,w:10,h:10}];
c.astromapItems=()=>c.state.annotations.filter(a=>a.astromap);
c.toEl=(x,y)=>({x,y});c.annColor=()=> '#123456';c.hexAlpha=()=> 'ordinary-fill';
c.svgEl=(tag,attrs)=>{if(tag==='polygon')polygons.push(attrs);return {addEventListener(){}};};
c.chip=()=>{};c.renderLandmarks=()=>{};c.panToAstromapRoi=a=>located.push(a.id);
c.$('ann-layer').replaceChildren=()=>{polygons.length=0;};
c.annotationsChanged=()=>c.renderAnnotations();
vm.runInContext(production('renderAnnotations')+'\n'+production('selectAstromapRoi')+'\n'+production('removeAnnotation'),c);
const selected=c.selectAstromapRoi('two'),colors=polygons.map(p=>({fill:p.fill,stroke:p.stroke}));
const absent=c.selectAstromapRoi('missing');
c.removeAnnotation('two');const afterMiddle=c.state.astromap.activeId;
c.removeAnnotation('three');const afterLast=c.state.astromap.activeId;
return {selected,absent,located,colors,afterMiddle,afterLast};
""")
    assert out["selected"] and not out["absent"] and out["located"] == ["two"]
    assert out["colors"] == [
        {"fill": "rgba(160,166,174,.15)", "stroke": "#a0a6ae"},
        {"fill": "rgba(255,217,102,.35)", "stroke": "#ffd966"},
        {"fill": "rgba(160,166,174,.15)", "stroke": "#a0a6ae"},
        {"fill": "ordinary-fill", "stroke": "#123456"},
    ]
    assert out["afterMiddle"] == "three" and out["afterLast"] == "one"


def test_roi_list_numbers_stay_ordered_and_click_updates_selected_row_and_location():
    out = _run_export(r"""
const f=fixture(),c=f.c,located=[];
function node() {
  const classes=new Set();
  return {children:[],attrs:{},classList:{toggle(k,on){on?classes.add(k):classes.delete(k);}},classes,
    setAttribute(k,v){this.attrs[k]=v;},append(...items){this.children.push(...items);},remove(){},
    insertBefore(item,before){this.children=this.children.filter(n=>n!==item);const i=this.children.indexOf(before);
      this.children.splice(i<0?this.children.length:i,0,item);}};
}
c.document.createElement=()=>node();c.state.astromap.rows=new Map();
Object.assign(c.state.info,{width:1000,height:500});
const list=node();f.nodes.set('astromap-list',list);
c.state.annotations=[{id:'one',type:'box',astromap:true,x:0,y:0,w:10,h:10,text:'first'},
  {id:'two',type:'box',astromap:true,x:20,y:20,w:10,h:10,text:'second'}];
c.astromapItems=()=>c.state.annotations;
c.frameOwnsRoi=()=>true;c.postAstromapState=()=>{};c.renderAnnotations=()=>{};
c.panToAstromapRoi=a=>located.push(a.id);
vm.runInContext(production('refreshAstromapPanel')+'\n'+production('selectAstromapRoi'),c);
c.refreshAstromapPanel();c.state.astromap.rows.get('two').number.onclick();
return {located,active:c.state.astromap.activeId,rows:[...c.state.astromap.rows.values()].map(r=>({
  number:r.number.textContent,note:r.note.value,pressed:r.number.attrs['aria-pressed'],selected:r.el.classes.has('selected')}))};
""")
    assert out == {"located": ["two"], "active": "two", "rows": [
        {"number": "1", "note": "first", "pressed": "false", "selected": False},
        {"number": "2", "note": "second", "pressed": "true", "selected": True},
    ]}


def test_imported_default_index_notes_display_once_without_changing_exported_notes():
    out = _run_export(r"""
const f=fixture(),c=f.c,labels=[];
c.state.viewer={};
c.state.annotations=[
  {id:'one',type:'box',astromap:true,astromapImportIndex:7,x:0,y:0,w:10,h:10,text:'7'},
  {id:'two',type:'box',astromap:true,astromapImportIndex:19,x:20,y:20,w:10,h:10,text:' 19 '},
  {id:'three',type:'box',astromap:true,astromapImportIndex:20,x:40,y:40,w:10,h:10,text:'capsule'},
  {id:'manual',type:'box',astromap:true,x:60,y:60,w:10,h:10,text:'4'},
  {id:'numeric-note',type:'box',astromap:true,astromapImportIndex:21,x:80,y:80,w:10,h:10,text:'50'},
  {id:'ordinary',type:'box',x:100,y:100,w:10,h:10,text:'6'},
];
const before=JSON.stringify(c.state.annotations);
c.toEl=(x,y)=>({x,y});c.annColor=()=> '#123456';c.hexAlpha=()=> 'ordinary-fill';
c.svgEl=()=>({addEventListener(){}});c.renderLandmarks=()=>{};
c.chip=(layer,x,y,text,id)=>labels.push({id,text});
c.$('ann-layer').replaceChildren=()=>{labels.length=0;};
vm.runInContext(production('renderAnnotations'),c);
c.renderAnnotations();
return {labels,unchanged:before===JSON.stringify(c.state.annotations),
  exported:c.window.Nd2Astromap.selectedRois(c.state.annotations).map(a=>a.note)};
""")
    assert out["labels"] == [
        {"id": "one", "text": "1"}, {"id": "two", "text": "2"},
        {"id": "three", "text": "3 · capsule"}, {"id": "manual", "text": "4 · 4"},
        {"id": "numeric-note", "text": "5 · 50"}, {"id": "ordinary", "text": "6"},
    ]
    assert out["unchanged"]
    assert out["exported"] == ["7", " 19 ", "capsule", "4", "50"]


def test_imported_default_index_note_is_hidden_in_list_and_can_be_replaced_with_a_note():
    out = _run_export(r"""
const f=fixture(),c=f.c;
function node() {
  return {children:[],attrs:{},classList:{toggle(){}},setAttribute(k,v){this.attrs[k]=v;},
    append(...items){this.children.push(...items);},remove(){},
    insertBefore(item,before){this.children.push(item);}};
}
c.document.createElement=()=>node();c.state.astromap.rows=new Map();
Object.assign(c.state.info,{width:1000,height:500});
f.nodes.set('astromap-list',node());
c.state.annotations=[{id:'imported',type:'box',astromap:true,astromapImportIndex:1,
  x:0,y:0,w:10,h:10,text:'1'}];
c.astromapItems=()=>c.state.annotations;c.frameOwnsRoi=()=>true;c.postAstromapState=()=>{};
let saves=0;c.annotationsChanged=()=>{saves++;c.refreshAstromapPanel();};
vm.runInContext(production('refreshAstromapPanel'),c);
c.refreshAstromapPanel();
const row=c.state.astromap.rows.get('imported');
const before={number:row.number.textContent,note:row.note.value,placeholder:row.note.placeholder,
  original:c.state.annotations[0].text,saves};
c.document.activeElement=row.note;row.note.value='stress granule';row.note.oninput();
c.document.activeElement=null;c.refreshAstromapPanel();
return {before,after:{note:row.note.value,stored:c.state.annotations[0].text,saves}};
""")
    assert out == {
        "before": {"number": "1", "note": "", "placeholder": "Note", "original": "1", "saves": 0},
        "after": {"note": "stress granule", "stored": "stress granule", "saves": 1},
    }


def test_clear_all_removes_only_astromap_regions_with_one_revision_render_and_save():
    pane = (STATIC / "index.html").read_text(encoding="utf-8")
    assert pane.index('id="astromap-list"') < pane.index('id="astromap-clear-all"') < pane.index('id="astromap-export"')
    assert re.search(r'id="astromap-clear-all"[^>]*>Clear all</button>', pane)
    out = _run_export(r"""
const f=fixture(),c=f.c,counts={render:0,list:0,panel:0,recovery:0,save:0,cancel:0},closed=[];
const manual=[{id:'manual-box',type:'box',x:1,y:2,w:3,h:4,text:'manual'},
  {id:'measurement',type:'line',x1:1,y1:2,x2:3,y2:4,text:'length'},
  {id:'pin',type:'pin',x:1,y:2,text:'landmark'}];
c.state.annotations=[...manual,
  {id:'imported',type:'box',astromap:true,astromapImportIndex:1,text:'1'},
  {id:'excluded',type:'box',astromap:true,astromapEnabled:false,text:'excluded'}];
c.state.astromap.activeId='imported';c.state.editingId='imported';
c.state.annRevision=5;c.state.annDirty=false;c.state.annAcceptedDrafts=new Set(['old']);
c.state.tool='astromap';c.state.cancelToolDrag=()=>counts.cancel++;
c.closeEditor=save=>{closed.push(save);c.state.editingId=null;};
c.renderAnnotations=()=>counts.render++;
c.rebuildAnnList=()=>counts.list++;
c.refreshAstromapPanel=()=>{counts.panel++;c.state.astromap.rows.clear();c.$('astromap-empty').hidden=false;c.refreshAstromapImport();};
c.state.astromap.rows=new Map([['imported',{}],['excluded',{}]]);
c.updateAnnotationRecoveryNotice=()=>counts.recovery++;
c.scheduleAnnSave=()=>counts.save++;
vm.runInContext([production('astromapItems'),production('clearAstromapRois'),
  production('annotationsChanged'),production('refreshAstromapImport')].join('\n'),c);
const cleared=c.clearAstromapRois(),again=c.clearAstromapRois();
return {cleared,again,counts,closed,kept:c.state.annotations,manual,active:c.state.astromap.activeId,
  revision:c.state.annRevision,dirty:c.state.annDirty,drafts:c.state.annAcceptedDrafts.size,
  rows:c.state.astromap.rows.size,empty:c.$('astromap-empty').hidden,
  disabled:c.$('astromap-clear-all').disabled,status:c.$('astromap-status').textContent};
""")
    assert out["cleared"] and not out["again"]
    assert out["counts"] == {"render": 1, "list": 1, "panel": 1, "recovery": 1, "save": 1, "cancel": 1}
    assert out["closed"] == [False] and out["kept"] == out["manual"]
    assert out["active"] is None and out["revision"] == 6 and out["dirty"] and out["drafts"] == 0
    assert out["rows"] == 0 and not out["empty"] and out["disabled"]
    assert out["status"] == "Cleared 2 Astromap regions."


def test_clear_all_respects_edit_locks_export_import_busy_and_keeps_manual_editor():
    out = _run_export(r"""
const changes=[c=>{c.locked=true;},c=>{c.ownerReady=false;},c=>{c.context=null;},
  c=>{c.imageReady=false;},c=>{c.state.quitPreparing=true;},
  c=>{c.state.astromap.exporting=true;},c=>{c.state.astromap.importReading=true;}];
const blocked=changes.map(change=>{
  const f=fixture(),c=f.c;
  vm.runInContext([production('astromapItems'),production('clearAstromapRois'),
    production('refreshAstromapImport')].join('\n'),c);
  let saved=0;c.annotationsChanged=()=>saved++;
  const before=JSON.stringify(c.state.annotations);change(c);c.refreshAstromapImport();
  return {result:c.clearAstromapRois(),disabled:c.$('astromap-clear-all').disabled,
    unchanged:before===JSON.stringify(c.state.annotations),saved};
});
const f=fixture(),c=f.c;let closed=0,saved=0;
c.state.annotations=[{id:'manual',type:'box',text:'keep'},
  {id:'region',type:'box',astromap:true,text:'remove'}];
c.state.editingId='manual';c.closeEditor=()=>closed++;c.annotationsChanged=()=>saved++;
vm.runInContext(production('astromapItems')+'\n'+production('clearAstromapRois'),c);
const result=c.clearAstromapRois();
return {blocked,manual:{result,closed,saved,editing:c.state.editingId,remaining:c.state.annotations}};
""")
    assert out["blocked"] == [{"result": False, "disabled": True, "unchanged": True, "saved": 0}] * 7
    assert out["manual"] == {"result": True, "closed": 0, "saved": 1, "editing": "manual",
                             "remaining": [{"id": "manual", "type": "box", "text": "keep"}]}


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
    assert "addAstromapRoi(rectangle)" in tools and "positionRubber(drawStart(), end)" in tools
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
    if(!nodes.has(id))nodes.set(id,{hidden:false,disabled:true,attrs:{},classList:{toggle(){}},
      setAttribute(k,v){this.attrs[k]=v;}});
    return nodes.get(id);
  }
  const c=vm.createContext({
    $,active:'a',readyFrames:new Set(['a','b']),quitPreparation:null,pairPicker:{open:false},
    astromapStates:new Map(),
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


def test_visible_icon_and_shortcut_toggle_active_panel_without_key_repeat():
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
    assert out["initial"] is False and out["beforeShortcut"] == 1
    assert out["first"] == out["repeat"] == {"prevented": 1, "stopped": 1}
    assert out["afterRepeat"] == 2 and out["displayed"] == {"hidden": False, "pressed": "true"}
    first = {"sid": "a", "data": {"nd2wsi": "astromap-toggle", "version": 2, "sid": "a", "paneInstanceId": "pane-a", "contextEpoch": 1, "spatialContextKey": "site-a", "open": False}}
    assert out["sent"] == [first, first, first,
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
const h=shellFixture();h.c.active=null;h.c.refreshAstromapButton();h.$('astromap-toggle').onclick();
process.stdout.write(JSON.stringify({blocked,afterValid,afterReload,missingIdentity:g.sent.length,
  noPane:{hidden:h.$('astromap-toggle').hidden,disabled:h.$('astromap-toggle').disabled,sent:h.sent.length}}));
""")
    assert out["blocked"] == [{"hidden": False, "sent": 0}] * 11
    assert out["afterValid"] == out["afterReload"] == 1
    assert out["missingIdentity"] == 0
    assert out["noPane"] == {"hidden": False, "disabled": True, "sent": 0}


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
  const nodes = new Map(), navigation = [], added = [], previews = [];
  const camera = {scale:1,x:0,y:0}, viewerHandlers = {}, windowHandlers = {};
  const toImage = point => ({x:(point.x-camera.x)/camera.scale,y:(point.y-camera.y)/camera.scale});
  const toElement = point => ({x:point.x*camera.scale+camera.x,y:point.y*camera.scale+camera.y});
  let mouseNavEnabled = true;
  function $(id) {
    if (!nodes.has(id)) nodes.set(id, {style:{},handlers:{},classList:{toggle(){},add(){},remove(){}},
      captured:new Set(),
      addEventListener(kind,fn) { this.handlers[kind]=fn; },
      setPointerCapture(id){this.captured.add(id);},
      hasPointerCapture(id){return this.captured.has(id);},
      releasePointerCapture(id){this.captured.delete(id);}});
    return nodes.get(id);
  }
  const c = vm.createContext({
    $, hidden:false,ready:true,
    window:{addEventListener:(kind,fn)=>windowHandlers[kind]=fn},
    state:{info:{width:1000,height:1000},tool:null,landmark:{active:false},
      viewer:{setMouseNavEnabled:enabled=>{mouseNavEnabled=enabled;navigation.push(enabled);},
        isMouseNavEnabled:()=>mouseNavEnabled,
        addHandler:(kind,fn)=>viewerHandlers[kind]=fn},
      windows:{astromap:{isHidden:()=>c.hidden}}},
    astromapReady:()=>c.ready,annotationOwnerReady:()=>true,annLocked:()=>false,
    activeFrameContext:()=>({key:'slide-a:generation-a'}),
    elementPoint:event=>({x:event.x,y:event.y}),imgPoint:toImage,
    imageToViewerElementPoint:toElement,viewerElementToImagePoint:toImage,
    positionRubber:(a,b)=>{
      a=toImage(a);b=toImage(b);
      const rectangle={x:Math.min(a.x,b.x),y:Math.min(a.y,b.y),w:Math.abs(b.x-a.x),h:Math.abs(b.y-a.y)};
      previews.push(rectangle);return rectangle;
    },
    addAstromapRoi:rect=>added.push(rect),
    renderAnnotations(){},clearRoi(){},wireRoiDims(){},wireAnnotationPanel(){},
  });
  const start = app.indexOf('const TOOL_BTN = {');
  vm.runInContext(app.slice(start, app.indexOf('\n};',start)+3),c);
  vm.runInContext(production('setTool') + '\n' + production('wireTools'),c);
  c.wireTools();
  function emit(kind,overrides={}) {
    const event={button:0,buttons:kind==='pointerup'?0:1,shiftKey:true,metaKey:false,ctrlKey:false,altKey:false,
      x:10,y:20,pointerId:1,pointerType:'mouse',isPrimary:true,prevented:0,stopped:0,
      preventDefault(){this.prevented++;},stopPropagation(){this.stopped++;},...overrides};
    $('stage').handlers[kind](event);
    return {prevented:event.prevented,stopped:event.stopped};
  }
  return {c,$,navigation,added,previews,camera,viewerHandlers,windowHandlers,emit};
}
"""


def test_shift_primary_drag_enters_astromap_only_with_open_ready_panel_and_no_other_modifier():
    out = _run(DRAG_SCRIPT + r"""
const blocked=[];
for (const {event={},update=()=>{}} of [
  {event:{button:2}}, {event:{shiftKey:false}},
  {event:{metaKey:true}}, {event:{ctrlKey:true}}, {event:{altKey:true}},
  {event:{pointerType:'touch'}},
  {update:f=>f.c.hidden=true}, {update:f=>f.c.ready=false},
]) {
  const f=fixture();update(f);
  const dispatched=f.emit('pointerdown',event);
  blocked.push({tool:f.c.state.tool,navigation:f.navigation,...dispatched});
}
const f=fixture(),dispatched=f.emit('pointerdown');
process.stdout.write(JSON.stringify({blocked,valid:{tool:f.c.state.tool,navigation:f.navigation,...dispatched}}));
""")
    assert out["blocked"] == [{"tool": None, "navigation": [], "prevented": 0, "stopped": 0}] * 8
    assert out["valid"] == {"tool": "astromap", "navigation": [True], "prevented": 1, "stopped": 1}


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
    assert out["tool"] is None and out["navigation"] == [True, True]
    assert out["rubber"] == "none"
    assert out["plainDown"] == out["plainMove"] == out["plainUp"] == {"prevented": 0, "stopped": 0}


@pytest.mark.parametrize("armed,busy", [(False, False), (True, False), (False, True), (True, True)])
def test_frame_linked_shift_drag_never_creates_astromap_roi_even_when_alignment_is_busy(armed, busy):
    out = _run(DRAG_SCRIPT + f"""
const f=fixture();
f.c.state.viewportRelay={{compare:{{enabled:true,frameLinked:true,nudgeBusy:{str(busy).lower()}}}}};
if ({str(armed).lower()}) f.c.setTool('astromap');
const events=['pointerdown','pointermove','pointerup'].map((kind,index)=>f.emit(kind,{{x:10+index*30,y:20+index*40}}));
process.stdout.write(JSON.stringify({{events,added:f.added,tool:f.c.state.tool,
  nav:f.c.state.viewer.isMouseNavEnabled(),captured:f.$('stage').captured.size}}));
""")
    assert out == {
        "events": [{"prevented": 0, "stopped": 0}] * 3,
        "added": [], "tool": "astromap" if armed else None, "nav": True, "captured": 0,
    }


@pytest.mark.parametrize("compare", [
    {"enabled": False, "frameLinked": False},
    {"enabled": True, "frameLinked": False, "linkMode": "scale"},
])
def test_unlinked_and_scale_only_compare_keep_astromap_shift_drawing(compare):
    out = _run(DRAG_SCRIPT + f"""
const f=fixture();f.c.state.viewportRelay={{compare:{json.dumps(compare)}}};
f.emit('pointerdown');f.emit('pointermove',{{x:80,y:110}});f.emit('pointerup',{{x:80,y:110}});
process.stdout.write(JSON.stringify({{added:f.added,tool:f.c.state.tool}}));
""")
    assert out == {"added": [{"x": 10, "y": 20, "w": 70, "h": 90}], "tool": None}


def test_armed_astromap_passes_plain_drag_and_touch_to_navigation():
    out = _run(DRAG_SCRIPT + r"""
const results=[];
for (const event of [{shiftKey:false},{pointerType:'touch'},{metaKey:true},{ctrlKey:true},{altKey:true},{button:2}]) {
  const f=fixture();f.c.setTool('astromap');
  const events=['pointerdown','pointermove','pointerup'].map((kind,index)=>f.emit(kind,{...event,x:20+index*30,y:20+index*40}));
  results.push({events,tool:f.c.state.tool,added:f.added,nav:f.c.state.viewer.isMouseNavEnabled(),navigation:f.navigation});
}
process.stdout.write(JSON.stringify(results));
""")
    assert out == [{
        "events": [{"prevented": 0, "stopped": 0}] * 3,
        "tool": "astromap", "added": [], "nav": True, "navigation": [True],
    }] * 6


def test_astromap_source_anchor_survives_mid_drag_zoom_and_pan():
    out = _run(DRAG_SCRIPT + r"""
const f=fixture();f.emit('pointerdown');f.emit('pointermove',{x:80,y:110});
Object.assign(f.camera,{scale:2,x:-20,y:-30});
f.viewerHandlers['update-viewport']();
const afterCameraChange=f.previews.at(-1),navDuringDrag=f.c.state.viewer.isMouseNavEnabled();
f.emit('pointerup',{x:140,y:190,shiftKey:false});
process.stdout.write(JSON.stringify({afterCameraChange,navDuringDrag,added:f.added,navigation:f.navigation}));
""")
    assert out["afterCameraChange"] == {"x": 10, "y": 20, "w": 40, "h": 50}
    assert out["added"] == [{"x": 10, "y": 20, "w": 70, "h": 90}]
    assert out["navDuringDrag"] and all(out["navigation"])


def test_astromap_foreign_pointer_cannot_move_finish_or_cancel_active_roi():
    out = _run(DRAG_SCRIPT + r"""
const f=fixture();f.emit('pointerdown');f.emit('pointermove',{x:80,y:110});
const before=f.previews.at(-1);
for (const kind of ['pointerdown','pointermove','pointerup','pointercancel','lostpointercapture'])
  f.emit(kind,{pointerId:2,isPrimary:false,x:400,y:500});
const after=f.previews.at(-1),tool=f.c.state.tool,addedBefore=f.added.slice();
f.emit('pointerup',{x:80,y:110});
process.stdout.write(JSON.stringify({before,after,tool,addedBefore,added:f.added}));
""")
    assert out["before"] == out["after"] == {"x": 10, "y": 20, "w": 70, "h": 90}
    assert out["tool"] == "astromap" and out["addedBefore"] == []
    assert out["added"] == [{"x": 10, "y": 20, "w": 70, "h": 90}]


@pytest.mark.parametrize("event", ["pointercancel", "lostpointercapture"])
def test_astromap_cancel_discards_preview_and_next_drag_has_fresh_anchor(event):
    out = _run(DRAG_SCRIPT + f"const cancelEvent={json.dumps(event)};" + r"""
const f=fixture();f.emit('pointerdown');f.emit('pointermove',{x:80,y:110});
f.emit(cancelEvent);
const cancelled={tool:f.c.state.tool,rubber:f.$('rubber').style.display,added:f.added.slice(),nav:f.c.state.viewer.isMouseNavEnabled()};
f.emit('pointerup',{x:80,y:110});
f.emit('pointerdown',{x:200,y:300});f.emit('pointerup',{x:240,y:360});
process.stdout.write(JSON.stringify({cancelled,added:f.added}));
""")
    assert out["cancelled"] == {"tool": None, "rubber": "none", "added": [], "nav": True}
    assert out["added"] == [{"x": 200, "y": 300, "w": 40, "h": 60}]
