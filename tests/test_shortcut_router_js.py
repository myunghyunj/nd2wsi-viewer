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
    type: spec.type,
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
  event.target.ownerDocument = {getSelection: () => entry.selection === undefined ? null : {
    isCollapsed: !!entry.selectionCollapsed, toString: () => entry.selection,
  }};
  if (entry.path) event.composedPath = () => entry.path.map(makeTarget);
  // Historical Command-key cases model a Mac, even when Node runs on Windows.
  const platform = entry.platform || 'MacIntel';
  return {
    typing: Router.isTypingEvent(event),
    focusControl: Router.isFocusControl(event.target),
    plain: Router.plainLetterForEvent(event),
    letter: Router.letterCode(event),
    panel: Router.panelForEvent(event, platform),
    orientationShortcut: Router.isOrientationShortcut(event, platform),
    orientation: Router.orientationForEvent(event, platform),
    tab: Router.tabIndexForEvent(event, platform),
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
            {"event": {"code": "KeyE", "key": "ㄷ"}},
            {"event": {"code": "KeyA", "key": "ㅁ", "metaKey": True}},
            {"event": {"code": "KeyC", "key": "ㅊ", "metaKey": True}},
        ]
    )
    assert [item["typing"] for item in out] == [False, False, False]
    assert [item["panel"] for item in out] == ["region", "annot", "channels"]
    assert [item["tab"] for item in out] == [None, None, None]


def test_letter_shortcuts_follow_latin_keycaps_and_fall_back_for_korean():
    out = _run(
        [
            {"event": {"code": "KeyP", "key": "e"}},
            {"event": {"code": "KeyQ", "key": "a", "metaKey": True}},
            {"event": {"code": "KeyI", "key": "c", "metaKey": True}},
            {"event": {"code": "KeyE", "key": "ㄷ"}},
        ]
    )
    assert [item["letter"] for item in out] == ["KeyE", "KeyA", "KeyC", "KeyE"]
    assert [item["panel"] for item in out] == ["region", "annot", "channels", "region"]


def test_text_entry_and_ime_composition_suppress_shortcuts():
    out = _run(
        [
            {"event": {"code": "KeyE"}, "target": {"tag": "INPUT"}},
            {"event": {"code": "KeyE"}, "target": {"tag": "textarea"}},
            {"event": {"code": "KeyE"}, "target": {"tag": "Select"}},
            {"event": {"code": "KeyE"}, "target": {"contentEditable": True}},
            {"event": {"code": "KeyE"}, "target": {"insideEditable": True}},
            {"event": {"code": "KeyE"}, "target": {"insideTextbox": True}},
            {"event": {"code": "KeyE", "isComposing": True}},
            {"event": {"code": "KeyE", "keyCode": 229}},
        ]
    )
    assert all(item["typing"] for item in out)
    assert all(item["panel"] is None and item["tab"] is None for item in out)


def test_checkbox_focus_allows_letters_but_keeps_native_space_and_focus_ownership():
    out = _run([
        {"target": {"tag": "INPUT", "type": "checkbox"}, "event": event}
        for event in [
            {"key": "e", "code": "KeyE"}, {"key": "ㄷ", "code": "KeyE"},
            {"key": " ", "code": "Space"}, {"key": "Spacebar"}, {"keyCode": 32},
            {"key": "e", "code": "KeyE", "isComposing": True},
            {"key": "e", "code": "KeyE", "keyCode": 229},
        ]
    ])
    assert [row["panel"] for row in out] == ["region", "region", None, None, None, None, None]
    assert [row["typing"] for row in out] == [False, False, True, True, True, True, True]
    assert all(row["focusControl"] for row in out)


@pytest.mark.parametrize("input_type", [None, "text", "number", "search", "email", "range", "radio"])
def test_other_input_controls_keep_their_existing_native_key_ownership(input_type):
    out = _run([{"target": {"tag": "INPUT", "type": input_type},
                 "event": {"key": "c", "code": "KeyC"}}])[0]
    assert out["typing"] and out["focusControl"]
    assert out["plain"] is None and out["panel"] is None


@pytest.mark.parametrize("target", [
    {"tag": "INPUT", "type": "text"}, {"tag": "SELECT"},
    {"tag": "TEXTAREA"}, {"contentEditable": True}, {"insideTextbox": True},
])
def test_shadow_retargeting_does_not_hide_text_entry_from_shortcut_guards(target):
    out = _run([{"target": {"tag": "DIV"}, "path": [target, {"tag": "DIV"}],
                 "event": {"key": "l", "code": "KeyL"}}])[0]
    assert out["typing"] and out["plain"] is None


def test_shadow_checkbox_allows_export_but_retains_space():
    out = _run([{"target": {"tag": "DIV"},
                 "path": [{"tag": "INPUT", "type": "checkbox"}, {"tag": "DIV"}],
                 "event": event}
                for event in [{"key": "e", "code": "KeyE"}, {"key": " ", "code": "Space"}]])
    assert [row["panel"] for row in out] == ["region", None]
    assert [row["typing"] for row in out] == [False, True]


def test_panel_shortcuts_reject_modifiers_repeats_and_prevented_events():
    out = _run(
        [
            {"event": {"code": "KeyE", "metaKey": True}},
            {"event": {"code": "KeyE", "ctrlKey": True}},
            {"event": {"code": "KeyE", "altKey": True}},
            {"event": {"code": "KeyE", "shiftKey": True}},
            {"event": {"code": "KeyE", "repeat": True}},
            {"event": {"code": "KeyE", "defaultPrevented": True}},
            {"event": {"code": "KeyX"}},
        ]
    )
    assert all(not item["typing"] for item in out)
    assert all(item["panel"] is None for item in out)


def test_command_r_and_f_rotate_clockwise_and_flip_horizontally():
    out = _run(
        [
            {"event": {"code": "KeyR", "key": "r", "metaKey": True}},
            {"event": {"code": "KeyF", "key": "f", "metaKey": True}},
            {"event": {"code": "KeyR", "key": "ㄱ", "metaKey": True}},
            {"event": {"code": "KeyF", "key": "ㄹ", "metaKey": True}},
            {"event": {"code": "KeyP", "key": "r", "metaKey": True}},
            {"event": {"code": "KeyU", "key": "F", "metaKey": True}},
        ]
    )
    assert [item["orientation"] for item in out] == [
        "rotate-right", "flip-horizontal", "rotate-right", "flip-horizontal",
        "rotate-right", "flip-horizontal",
    ]
    assert all(item["orientationShortcut"] for item in out)
    assert all(item["panel"] is None and item["tab"] is None for item in out)


@pytest.mark.parametrize("code", ["KeyR", "KeyF"])
def test_orientation_shortcuts_require_command_alone_and_reject_repeats(code):
    out = _run(
        [
            {"event": {"code": code}},
            {"event": {"code": code, "ctrlKey": True}},
            {"event": {"code": code, "metaKey": True, "ctrlKey": True}},
            {"event": {"code": code, "metaKey": True, "altKey": True}},
            {"event": {"code": code, "metaKey": True, "shiftKey": True}},
            {"event": {"code": code, "metaKey": True, "repeat": True}},
            {"event": {"code": code, "metaKey": True, "defaultPrevented": True}},
            {"event": {"code": "KeyA", "metaKey": True}},
        ]
    )
    assert all(item["orientation"] is None for item in out)
    assert [item["orientationShortcut"] for item in out] == [
        False, False, False, False, False, True, False, False,
    ]
    assert out[0]["panel"] is None


@pytest.mark.parametrize("code", ["KeyR", "KeyF"])
def test_orientation_shortcuts_respect_text_entry_and_ime_composition(code):
    out = _run(
        [
            {"event": {"code": code, "metaKey": True}, "target": target}
            for target in [
                {"tag": "INPUT"},
                {"tag": "textarea"},
                {"tag": "Select"},
                {"contentEditable": True},
                {"insideEditable": True},
                {"insideTextbox": True},
            ]
        ]
        + [
            {"event": {"code": code, "metaKey": True, "isComposing": True}},
            {"event": {"code": code, "metaKey": True, "keyCode": 229}},
        ]
    )
    assert all(item["typing"] for item in out)
    assert all(item["orientation"] is None for item in out)
    assert all(not item["orientationShortcut"] for item in out)


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


def test_windows_control_shortcuts_preserve_ime_and_modifier_guards():
    out = _run([
        {"platform": "Win32", "event": {"code": "KeyR", "key": "ㄱ", "ctrlKey": True}},
        {"platform": "Windows", "event": {"code": "KeyF", "ctrlKey": True}},
        {"platform": "Win32", "event": {"code": "Numpad3", "ctrlKey": True}},
        {"platform": "Win32", "event": {"code": "KeyR", "metaKey": True}},
        {"platform": "Win32", "event": {"code": "KeyF", "ctrlKey": True, "altKey": True}},
        {"platform": "Win32", "event": {"code": "Digit1", "ctrlKey": True, "metaKey": True}},
        {"platform": "Win32", "event": {"code": "KeyR", "ctrlKey": True, "isComposing": True}},
        {"platform": "Win32", "event": {"code": "KeyF", "ctrlKey": True}, "target": {"tag": "INPUT"}},
    ])
    assert [item["orientation"] for item in out] == ["rotate-right", "flip-horizontal", None, None, None, None, None, None]
    assert [item["tab"] for item in out] == [None, None, 2, None, None, None, None, None]


def test_shortcut_labels_match_the_windows_keyboard():
    result = subprocess.run(
        [NODE, "-e", "const R=require(process.argv[1]); console.log(R.formatShortcutText('Export (⌘⇧E); Option-drag', 'Win32'));", str(MODULE)],
        capture_output=True, text=True, check=True, timeout=20,
    )
    assert result.stdout.strip() == "Export (Ctrl+Shift+E); Alt-drag"


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
                "event": {"code": "KeyC", "metaKey": True},
                "target": {"contentEditable": False},
            }
        ]
    )
    assert out == [
        {
            "typing": False,
            "focusControl": False,
            "plain": None,
            "letter": "KeyC",
            "panel": "channels",
            "orientationShortcut": False,
            "orientation": None,
            "tab": None,
        }
    ]


def test_production_pages_use_the_router_and_document_the_same_shortcuts():
    app = APP.read_text(encoding="utf-8")
    index = INDEX.read_text(encoding="utf-8")
    shell_js = SHELL_JS.read_text(encoding="utf-8")
    shell_html = SHELL_HTML.read_text(encoding="utf-8")
    readme = README.read_text(encoding="utf-8")

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

    assert '| `⌘C` | show or hide Channels & LUTs |' in readme
    assert '| `E` | show or hide Export |' in readme
    assert '| `⌘A` | show or hide Annotations |' in readme
    assert '| `⌘1` … `⌘9` | switch to that tab |' in readme
    assert "`⌘C` `⌘R` `⌘A`" not in readme
    assert 'id="tb-channels" title="Show or hide Channels &amp; LUTs (⌘C)" aria-keyshortcuts="Meta+C"' in index
    assert 'id="tb-region" title="Show or hide Export (E)" aria-keyshortcuts="E"' in index
    assert 'id="tb-annot" title="Show or hide Annotations (⌘A)" aria-keyshortcuts="Meta+A"' in index
    info_button = index.split('id="tb-info"', 1)[1].split('</button>', 1)[0]
    assert 'aria-label="Slide Info"' in info_button and 'aria-keyshortcuts="Meta+I"' in info_button
    assert '<span>Info</span>' not in info_button
    assert '<span class="win-title">Export</span>' in index
    assert 'id="roi-toggle" class="btn" title="Drag on the slide to mark a region"' in index


@pytest.mark.parametrize("platform,modifier", [("MacIntel", "metaKey"), ("Win32", "ctrlKey")])
@pytest.mark.parametrize("code,panel", [("KeyC", "channels"), ("KeyA", "annot")])
def test_command_panels_require_the_platform_command_key(platform, modifier, code, panel):
    other = "ctrlKey" if modifier == "metaKey" else "metaKey"
    modifiers = [{modifier: True}, {}, {other: True}, {modifier: True, other: True},
                 {modifier: True, "shiftKey": True}, {modifier: True, "altKey": True},
                 {modifier: True, "repeat": True}, {modifier: True, "defaultPrevented": True}]
    out = _run([{"platform": platform, "event": {"code": code, **flags}} for flags in modifiers])
    assert [row["panel"] for row in out] == [panel] + [None] * 7


@pytest.mark.parametrize("code", ["KeyC", "KeyA"])
@pytest.mark.parametrize("target", [{"tag": "INPUT"}, {"tag": "INPUT", "type": "checkbox"},
                                    {"tag": "TEXTAREA"}, {"tag": "SELECT"},
                                    {"contentEditable": True}, {"insideEditable": True},
                                    {"insideTextbox": True}])
def test_native_copy_and_select_all_keep_control_ownership(code, target):
    out = _run([{"event": {"code": code, "metaKey": True}, "target": target}])[0]
    assert out["panel"] is None


@pytest.mark.parametrize("code", ["KeyC", "KeyA"])
def test_shadow_checkbox_keeps_native_copy_and_select_all(code):
    out = _run([{"event": {"code": code, "metaKey": True},
                 "path": [{"tag": "INPUT", "type": "checkbox"}, {"tag": "DIV"}]}])[0]
    assert out["panel"] is None


@pytest.mark.parametrize("code,panel", [("KeyC", "channels"), ("KeyA", "annot")])
def test_native_copy_and_select_all_keep_meaningful_selection(code, panel):
    out = _run([{"event": {"code": code, "metaKey": True}, **selection}
                for selection in [{"selection": "selected metadata"}, {"selection": "한글"},
                                  {"selection": "  \n "}, {"selection": ""},
                                  {"selection": "old selection", "selectionCollapsed": True}]])
    assert [row["panel"] for row in out] == [None, None, panel, panel, panel]


def test_windows_labels_localize_accessible_shortcut_names_too():
    script = r"""
    const R=require(process.argv[1]);
    const entries=['Meta+C','Meta+A','Meta+I','E'].map(shortcut=>({
      title:'Panels (⌘C, ⌘A); Export (E)',shortcut,
      getAttribute(){return this.shortcut;},setAttribute(name,value){this.shortcut=value;},
    }));
    R.localizeLabels({documentElement:{classList:{toggle(){}}},querySelectorAll:()=>entries},'Win32');
    process.stdout.write(JSON.stringify(entries));
    """
    result = subprocess.run([NODE, "-e", script, str(MODULE)], capture_output=True,
                            text=True, check=True, timeout=20)
    out = json.loads(result.stdout)
    assert [row["shortcut"] for row in out] == ["Control+C", "Control+A", "Control+I", "E"]
    assert all(row["title"] == "Panels (Ctrl+C, Ctrl+A); Export (E)" for row in out)
