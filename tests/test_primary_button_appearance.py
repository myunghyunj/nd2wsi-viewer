"""Keep primary light-mode actions distinguishable from neutral buttons."""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

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


def test_light_primary_background_overrides_neutral_with_readable_blue():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    # .btn.primary alone loses to :root.light .btn, even if declared later.
    # Require an explicit background at the light primary selector, not just
    # white text. The packaged WKWebView check covers the actual cascade.
    rule = re.search(r":root\.light \.btn\.primary\s*\{([^}]+)\}", css)
    assert rule is not None
    declarations = dict(re.findall(r"([\w-]+)\s*:\s*([^;]+);", rule[1]))
    assert declarations["color"] in {"#fff", "#ffffff"}
    gradient = re.fullmatch(
        r"linear-gradient\((#[0-9a-fA-F]{6}),\s*(#[0-9a-fA-F]{6})\)",
        declarations["background"],
    )
    assert gradient is not None
    for stop in gradient.groups():
        rgb = [int(stop[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        assert rgb[2] > rgb[1] > rgb[0], "Primary background must remain blue"
        linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in rgb]
        luminance = sum(v * weight for v, weight in zip(linear, (0.2126, 0.7152, 0.0722)))
        assert 1.05 / (luminance + 0.05) >= 4.5
