"""Keyboard shortcuts, exercised through the production JavaScript router."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = (
    Path(__file__).resolve().parents[1]
    / "nd2wsi"
    / "static"
    / "shortcut-router-v1.js"
)
ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "nd2wsi" / "static" / "app.js"
INDEX = ROOT / "nd2wsi" / "static" / "index.html"
SHELL_JS = ROOT / "nd2wsi" / "static" / "shell-v1.js"
SHELL_HTML = ROOT / "nd2wsi" / "static" / "shell.html"
README = ROOT / "README.md"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SCRIPT = r"""
const Router = require(process.argv[1]);
const cases = JSON.parse(process.argv[2]);

function makeTarget(spec) {
  spec = spec || {};
  return {
    tagName: spec.tag || "DIV",
    isContentEditable: !!spec.contentEditable,
    parentElement: null,
    closest(selector) {
      if (spec.insideEditable && selector.includes("contenteditable")) return {};
      if (spec.insideTextbox && selector.includes('role="textbox"')) return {};
      return null;
    },
  };
}

const out = cases.map((entry) => {
  const event = {...entry.event, target: makeTarget(entry.target)};
  return {
    typing: Router.isTypingEvent(event),
    letter: Router.letterCode(event),
    panel: Router.panelForEvent(event),
    tab: Router.tabIndexForEvent(event),
  };
});
process.stdout.write(JSON.stringify(out));
"""


def _run(cases):
    result = subprocess.run(
        [NODE, "-e", SCRIPT, str(MODULE), json.dumps(cases)],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    return json.loads(result.stdout)


def test_panel_shortcuts_use_physical_codes_under_a_korean_input_source():
    out = _run(
        [
            {"event": {"code": "KeyR", "key": "ㄱ"}},
            {"event": {"code": "KeyA", "key": "ㅁ"}},
            {"event": {"code": "KeyC", "key": "ㅊ"}},
        ]
    )
    assert [item["typing"] for item in out] == [False, False, False]
    assert [item["panel"] for item in out] == ["region", "annot", "channels"]
    assert [item["tab"] for item in out] == [None, None, None]


def test_letter_shortcuts_follow_latin_keycaps_and_fall_back_for_korean():
    out = _run(
        [
            {"event": {"code": "KeyP", "key": "r"}},
            {"event": {"code": "KeyQ", "key": "a"}},
            {"event": {"code": "KeyI", "key": "c"}},
            {"event": {"code": "KeyR", "key": "ㄱ"}},
        ]
    )
    assert [item["letter"] for item in out] == ["KeyR", "KeyA", "KeyC", "KeyR"]
    assert [item["panel"] for item in out] == ["region", "annot", "channels", "region"]


def test_text_entry_and_ime_composition_suppress_shortcuts():
    out = _run(
        [
            {"event": {"code": "KeyR"}, "target": {"tag": "INPUT"}},
            {"event": {"code": "KeyR"}, "target": {"tag": "textarea"}},
            {"event": {"code": "KeyR"}, "target": {"tag": "Select"}},
            {"event": {"code": "KeyR"}, "target": {"contentEditable": True}},
            {"event": {"code": "KeyR"}, "target": {"insideEditable": True}},
            {"event": {"code": "KeyR"}, "target": {"insideTextbox": True}},
            {"event": {"code": "KeyR", "isComposing": True}},
            {"event": {"code": "KeyR", "keyCode": 229}},
        ]
    )
    assert all(item["typing"] for item in out)
    assert all(item["panel"] is None and item["tab"] is None for item in out)


def test_panel_shortcuts_reject_modifiers_repeats_and_prevented_events():
    out = _run(
        [
            {"event": {"code": "KeyR", "metaKey": True}},
            {"event": {"code": "KeyR", "ctrlKey": True}},
            {"event": {"code": "KeyR", "altKey": True}},
            {"event": {"code": "KeyR", "shiftKey": True}},
            {"event": {"code": "KeyR", "repeat": True}},
            {"event": {"code": "KeyR", "defaultPrevented": True}},
            {"event": {"code": "KeyX"}},
        ]
    )
    assert all(not item["typing"] for item in out)
    assert all(item["panel"] is None for item in out)


def test_command_digits_select_tabs_and_accept_the_numeric_keypad():
    out = _run(
        [
            {"event": {"code": "Digit1", "metaKey": True}},
            {"event": {"code": "Digit9", "metaKey": True}},
            {"event": {"code": "Numpad4", "metaKey": True}},
        ]
    )
    assert [item["tab"] for item in out] == [0, 8, 3]
    assert all(item["panel"] is None for item in out)


def test_tab_shortcuts_reject_wrong_modifiers_targets_and_keys():
    out = _run(
        [
            {"event": {"code": "Digit1"}},
            {"event": {"code": "Digit1", "ctrlKey": True}},
            {"event": {"code": "Digit1", "metaKey": True, "ctrlKey": True}},
            {"event": {"code": "Digit1", "metaKey": True, "shiftKey": True}},
            {"event": {"code": "Digit1", "metaKey": True, "altKey": True}},
            {"event": {"code": "Digit1", "metaKey": True, "repeat": True}},
            {"event": {"code": "Digit0", "metaKey": True}},
            {
                "event": {"code": "Digit1", "metaKey": True},
                "target": {"tag": "INPUT"},
            },
            {"event": {"code": "Digit1", "metaKey": True, "isComposing": True}},
            {"event": {"code": "Digit1", "metaKey": True, "defaultPrevented": True}},
        ]
    )
    assert all(item["tab"] is None for item in out)


def test_contenteditable_false_does_not_hide_a_panel_shortcut():
    out = _run(
        [
            {
                "event": {"code": "KeyC"},
                "target": {"contentEditable": False},
            }
        ]
    )
    assert out == [
        {"typing": False, "letter": "KeyC", "panel": "channels", "tab": None}
    ]


def test_production_pages_use_the_router_and_document_the_same_shortcuts():
    app = APP.read_text()
    index = INDEX.read_text()
    shell_js = SHELL_JS.read_text()
    shell_html = SHELL_HTML.read_text()
    readme = README.read_text()

    assert index.index("shortcut-router-v1.js") < index.index("app.js")
    assert shell_html.index("shortcut-router-v1.js") < shell_html.index("shell-v1.js")
    assert "shortcuts.panelForEvent(ev)" in app
    assert "shortcuts.letterCode(ev)" in app
    assert "shortcuts.tabIndexForEvent(ev)" in app
    assert "ShortcutRouter.tabIndexForEvent(event)" in shell_js
    assert 'nd2wsi: "tab-shortcut-state"' in shell_js
    assert 'event.data.nd2wsi === "tab-shortcut-state"' in app
    assert "tabIndex < state.tabCount" in app
    assert '["Digit0", "Numpad0"].includes(ev.code)' in app
    missing_guard = 'if (index === null || !slides[index]) return;'
    assert shell_js.index(missing_guard) < shell_js.index("event.preventDefault();", shell_js.index(missing_guard))

    assert '| `C` | show or hide Channels & LUTs |' in readme
    assert '| `R` | show or hide Region |' in readme
    assert '| `A` | show or hide Annotations |' in readme
    assert '| `⌘1` … `⌘9` | switch to that tab |' in readme
    assert "`⌘C` `⌘R` `⌘A`" not in readme
    assert 'id="tb-channels" title="Show or hide Channels &amp; LUTs (C)"' in index
    assert 'id="roi-toggle" class="btn" title="Drag on the slide to mark a region"' in index
