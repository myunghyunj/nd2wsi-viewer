"""Exercise Info image allowlisting, accessible expansion and failure cleanup."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
APP = Path(__file__).resolve().parents[1] / "nd2wsi/static/app.js"
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def test_info_images_expand_in_place_and_only_fetch_allowed_names():
    script = r"""
const fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
function node(tag) {
  const n = {tag, children: [], attrs: {}, handlers: {}, hidden: false,
    append(...xs) { for (const x of xs) {x.parent = this; this.children.push(x);} },
    replaceChildren() {this.children = [];},
    setAttribute(k, v) {this.attrs[k] = v;},
    addEventListener(k, f) {this.handlers[k] = f;},
    remove() {this.parent.children = this.parent.children.filter(x => x !== this);}
  };
  n.classList = {toggle(k, v) {n[k] = v;}};
  return n;
}
const section = node('section'), container = node('div');
let fits = 0;
const context = {document: {createElement: node}, URLSearchParams,
  state: {info: {generation: 'source/identity'}, windows: {info: {fitContent() {fits++;}}}},
  $: id => id === 'info-associated' ? container : section};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function renderAssociatedImages('),
  source.indexOf('async function copyPlainText(')), context);
context.renderAssociatedImages(['associated-1', 'associated-17', '../patient', 'unknown']);
const figure = container.children[0], button = figure.children[0], image = button.children[0];
const initial = {count: container.children.length, hidden: section.hidden,
  expanded: button.attrs['aria-expanded'], src: image.src, caption: figure.children[1].textContent};
button.handlers.click();
const expanded = {value: button.attrs['aria-expanded'], span: figure.expanded,
  src: image.src, caption: figure.children[1].textContent};
button.handlers.click();
const reduced = {value: button.attrs['aria-expanded'], span: figure.expanded};
image.handlers.error();
const failed = {hidden: section.hidden, count: container.children.length};
context.renderAssociatedImages([]);
console.log(JSON.stringify({initial, expanded, reduced, failed, fits}));
"""
    result = subprocess.run(
        [NODE, "-e", script, str(APP)], check=True, capture_output=True,
        text=True, encoding="utf-8",
    )
    data = json.loads(result.stdout)
    assert data["initial"] == {
        "count": 1, "hidden": False, "expanded": "false",
        "src": "api/associated/associated-1.jpg?g=source%2Fidentity",
        "caption": "Embedded image 1 · Enlarge",
    }
    assert data["expanded"] == {
        "value": "true", "span": True,
        "src": "api/associated/associated-1.jpg?g=source%2Fidentity&detail=1",
        "caption": "Embedded image 1 · Reduce",
    }
    assert data["reduced"] == {"value": "false", "span": False}
    assert data["failed"] == {"hidden": True, "count": 0}
    assert data["fits"] == 2
