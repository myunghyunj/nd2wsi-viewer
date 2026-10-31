"""Compact viewer chrome keeps detailed metadata in the live status popover."""

import colorsys
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from node_host import run_node

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi/static"


class Layout(HTMLParser):
    """Record element ancestry without requiring a browser or a DOM package."""

    def __init__(self, html):
        super().__init__()
        self.stack = []
        self.ids = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        element_id = attrs.get("id")
        if element_id:
            assert element_id not in self.ids, element_id
            self.ids[element_id] = {"tag": tag, "attrs": attrs,
                                    "ancestors": [item[1] for item in self.stack if item[1]]}
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append((tag, element_id))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return


def test_toolbar_and_footer_show_the_compact_readouts_while_details_live_in_the_popover():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    nodes = Layout(html).ids
    for element_id in ("source-badge", "file-name", "file-dims"):
        assert "toolbar" in nodes[element_id]["ancestors"], element_id
    for element_id in ("save-status-summary", "save-status-details", "storage-badge", "calibration-badge",
                       "hw-cell", "plate-cache-cell", "export-cell", "plane-note"):
        assert "save-status-tooltip" in nodes[element_id]["ancestors"], element_id
        assert "statusbar" not in nodes[element_id]["ancestors"], element_id
        assert "toolbar" not in nodes[element_id]["ancestors"], element_id
    assert "statusbar" not in nodes["pos-um"]["ancestors"]
    assert "hidden" in nodes["pos-um"]["attrs"]
    assert nodes["save-status-tooltip"]["attrs"]["role"] == "tooltip"
    assert "hidden" in nodes["save-status-tooltip"]["attrs"]
    footer = html[html.index('<footer id="statusbar">'):html.index("</footer>")]
    assert re.findall(r'<span class="label">([^<]+)</span>', footer) == ["Zoom", "Pixel", "Level"]
    positions = [footer.index(f'id="{element_id}"') for element_id in
                 ("zoom-val", "pos-px", "level-lamps", "scalebar-wrap", "save-status")]
    assert positions == sorted(positions)
    assert nodes["save-status"]["attrs"]["aria-describedby"] == "save-status-tooltip"


def test_warning_signal_has_an_orange_color_and_a_distinct_glyph_in_both_themes():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    colors = re.findall(r"--save-warn:\s*rgb\(\s*(\d+),\s*(\d+),\s*(\d+)\s*\)", css)
    assert len(colors) == 2
    for color in colors:
        hue, saturation, value = colorsys.rgb_to_hsv(*(int(component) / 255 for component in color))
        assert 20 <= hue * 360 <= 45 and saturation > 0.8 and value > 0.8, color
    for selector in ('.save-status[data-state="failed"]', '.save-status[data-state="conflict"]',
                     '.save-status[data-warning="true"]'):
        rule = re.search(re.escape(selector) + r"\s*\{([^}]+)\}", css)
        assert rule and "color: var(--save-warn)" in rule[1], selector
    assert re.search(r'\.save-status\[data-warning="true"\]\s+\.save-check\s*\{\s*display:\s*none', css)
    assert re.search(r'\.save-status\[data-warning="true"\]\s+\.save-alert\s*\{\s*display:\s*block', css)
    popover = re.search(r"#save-status-tooltip\s*\{([^}]+)\}", css)[1]
    assert "pointer-events: auto" in popover and "overflow: auto" in popover
    assert "max-height:" in popover and "max-width:" in popover


HARNESS = r"""
const fs=require('fs'),path=require('path'),vm=require('vm');
const source=fs.readFileSync(path.join(process.argv[1],'app.js'),'utf8').replace(/\r\n/g,'\n');
function element(){return {textContent:'',hidden:false,dataset:{},children:[],
  append(...items){this.children.push(...items)},replaceChildren(...items){this.children=[...items]},
  classList:{values:new Set(),toggle(name,enabled){enabled?this.values.add(name):this.values.delete(name)}}}}
const nodes=Object.fromEntries(['source-badge','file-dims','storage-badge','calibration-badge','zoom-val',
  'lamp-0','lamp-1','pos-px','pixel-hud','pixel-hud-coord','pixel-hud-stage','pixel-hud-values','pixel-hud-note']
  .map(id=>[id,element()]));
const state={info:null,saveStatus:null,plate:null,pixel:{cursor:null,result:null,resultKey:null,hudVisible:true}};
let density=1,gridZoom=.25,frameKey='current';
const scaleCalls=[],positions=[];
const context={state,$:id=>nodes[id],document:{createElement:()=>element()},window:{devicePixelRatio:2},
  fmtInt:value=>Math.round(value).toLocaleString('en-US'),fmtUm:value=>value+' µm',
  deviceZoom:()=>density,activeLevel:()=>0,levelForDensity:()=>1,plateCellZoom:()=>gridZoom,
  currentImageZoom:()=>.5,updateScalebar:zoom=>scaleCalls.push(zoom),
  activeFrameContext:()=>({key:frameKey}),pixelSize:()=>state.info.pixelSizeUm,
  positionPixelHud:point=>positions.push(point)};
vm.createContext(context);
function load(start,end){
  const first=source.indexOf(start),last=source.indexOf(end,first);
  if(first<0||last<0)throw new Error('source bounds not found: '+start);
  vm.runInContext(source.slice(first,last),context);
}
load('function kindMark(', 'function planeNote(');
load('function setText(', 'function updateCursor(');
load('function pixelResultMatchesCursor(', 'function positionPixelHud(');
const info=changes=>({kind:'slide',width:2044,height:2048,dtype:'uint16',channels:[{name:'C0'},{name:'C1'}],
  calibrated:true,storage:'compact',pixelSizeUm:[.1,.1],levels:[{path:'0'},{path:'1'}],...changes});
"""


def run(body):
    result = run_node(HARNESS + "process.stdout.write(JSON.stringify((()=>{" + body + "})()));", STATIC)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("changes,kind,channels,title", [
    ({}, "2D SLIDE", 2, "2,044 × 2,048 px · 2 ch"),
    ({"rgb": True, "dtype": "uint8", "channels": [{"name": "R"}, {"name": "G"}, {"name": "B"}]},
     "2D SLIDE", 3, "2,044 × 2,048 px · 3 ch"),
    ({"kind": "plate", "plate": {"P": 6, "Z": 3}}, "3D PLATE", 2, "Dimensions of each site: 2,044 × 2,048 px · 2 ch"),
])
def test_header_metadata_keeps_dimensions_and_channel_count_for_slides_rgb_and_plates(changes, kind, channels, title):
    out = run(f"""
state.info=info({json.dumps(changes)});context.fillContextBadges(state.info);
return {{kind:nodes['source-badge'].textContent,dimensions:nodes['file-dims'].textContent,
  hidden:nodes['file-dims'].hidden,title:nodes['file-dims'].title}};
""")
    assert out == {"kind": kind, "dimensions": f"2,044 × 2,048 px · {channels} ch", "hidden": False, "title": title}


def test_zoom_readout_uses_percent_for_image_and_plate_views_without_the_old_one_to_one_suffix():
    out = run("""
state.info=info();context.updateReadout();
const image={zoom:nodes['zoom-val'].textContent,active:[...nodes['lamp-0'].classList.values]};
state.plate={focus:null};context.updateReadout();
const plate={zoom:nodes['zoom-val'].textContent,active:[...nodes['lamp-1'].classList.values]};
return {image,plate,scaleCalls};
""")
    assert out == {"image": {"zoom": "100 %", "active": ["active"]},
                   "plate": {"zoom": "50 %", "active": ["active"]}, "scaleCalls": [0.5, 0.25]}


@pytest.mark.parametrize("hud_visible", [True, False])
def test_footer_pixel_readout_is_coordinates_only_while_the_inspector_keeps_raw_values(hud_visible):
    out = run(f"""
state.info=info();state.pixel={{cursor:{{x:1234,y:567,point:{{x:10,y:20}}}},resultKey:'current',
  hudVisible:{json.dumps(hud_visible)},result:{{x:1234,y:567,um:[123.4,56.7],probed_level:0,
    values:[{{name:'C0',value:65535}},{{name:'C1',value:3.125}}]}}}};
context.renderPixelInspector();
return {{footer:nodes['pos-px'].textContent,title:nodes['pos-px'].title,hudHidden:nodes['pixel-hud'].hidden,
  hudCoord:nodes['pixel-hud-coord'].textContent,hudStage:nodes['pixel-hud-stage'].textContent,
  hudValues:nodes['pixel-hud-values'].children.map(row=>row.children.map(child=>child.textContent)),
  hudNote:nodes['pixel-hud-note'].textContent,positions}};
""")
    assert out["footer"] == out["title"] == "1,234 , 567"
    assert out["hudHidden"] is not hud_visible
    if hud_visible:
        assert out["hudCoord"] == "1,234 , 567 px"
        assert out["hudStage"] == "123.4 µm , 56.7 µm"
        assert out["hudValues"] == [["C0", "65,535"], ["C1", "3.125"]]
        assert out["hudNote"] == "Native pixel · L0" and out["positions"] == [{"x": 10, "y": 20}]
    else:
        assert out["hudValues"] == [] and out["positions"] == []
