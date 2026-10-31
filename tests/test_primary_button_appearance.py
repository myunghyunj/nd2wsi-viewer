"""Keep primary light-mode actions distinguishable from neutral buttons."""

from html.parser import HTMLParser
from pathlib import Path

import pytest
from test_theme_materials_js import run as run_material_check

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi" / "static"


class Buttons(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.classes = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "button" and "id" in attrs:
            self.classes[attrs["id"]] = set(attrs.get("class", "").split())


@pytest.mark.parametrize("button_id", ["ann-done", "roi-dl-nd2"])
def test_primary_actions_use_the_shared_primary_style(button_id):
    buttons = Buttons((STATIC / "index.html").read_text(encoding="utf-8"))
    assert {"btn", "primary"} <= buttons.classes[button_id]
    assert "primary" not in buttons.classes["ann-delete"]


@pytest.mark.parametrize("state,token", [("default", "--primary-fill"),
                                         ("hover", "--primary-hover"),
                                         ("active", "--primary-pressed")])
def test_light_primary_background_overrides_neutral_with_readable_blue(state, token):
    # Evaluate specificity across both pane stylesheets: a light neutral rule
    # must not override the primary just because its fill comes from a token.
    run_material_check(r"""
const css = read('style.css') + '\n' + read('native-v1.css');
const tokens = theme(true);
const primary = controlStyle(css, ['button', '.btn', '.btn.primary'], spec.state, true);
const neutral = controlStyle(css, ['button', '.btn'], spec.state, true);
assert.equal(primary.background, `var(${spec.token})`);
assert.equal(primary.color, 'var(--primary-ink)');
assert.notEqual(resolve(primary.background, tokens), resolve(neutral.background, tokens));
function rgb(value) {
  assert.match(value, /^#[\da-f]{6}$/i);
  return [1, 3, 5].map(index => parseInt(value.slice(index, index + 2), 16));
}
const background = rgb(resolve(primary.background, tokens));
const ink = rgb(resolve(primary.color, tokens));
assert.ok(background[2] > background[1] && background[1] > background[0],
  'Primary background must remain blue');
assert.ok(contrastRatio(ink, background) >= 4.5, 'Primary label must remain readable');
""", {"state": state, "token": token})
