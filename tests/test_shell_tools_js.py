"""Top slide tools share a disclosure and route only to the current source pane."""

from html.parser import HTMLParser

import pytest
from test_compare_orientation_js import NODE, STATIC, run

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


class Elements(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.stack = []
        self.elements = {}
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.elements[attrs["id"]] = (attrs, [item[1] for item in self.stack])
        if tag not in {"meta", "link", "input", "br", "img", "hr"}:
            self.stack.append((tag, attrs.get("id")))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                self.stack = self.stack[:index]
                break


def test_tools_share_the_top_group_and_lower_toolbar_has_no_duplicate_proxies():
    shell = Elements((STATIC / "shell.html").read_text(encoding="utf-8")).elements
    lower = Elements((STATIC / "index.html").read_text(encoding="utf-8")).elements
    for key in ["translucent-toggle", "astromap-toggle", "shading-correction", "compare-toggle", "info-toggle"]:
        assert "shell-tools" in shell[key][1]
    assert "hidden" in shell["shell-tools"][0]
    assert shell["shell-tools-toggle"][0]["aria-expanded"] == "false"
    assert shell["shell-tools-toggle"][0]["aria-controls"] == "shell-tools"
    assert "tb-info" not in lower and "tb-translucent" not in lower
    assert all(key in lower for key in ["tb-channels", "tb-region", "tb-annot", "tb-theme", "win-info"])
    style = (STATIC / "native-shell-v1.css").read_text(encoding="utf-8")
    assert 'url("vendor/icons8-win10/angle-left.svg")' in style
    assert '#shell-tools-toggle[aria-expanded="true"] .tools-chevron' in style
    assert 'url("vendor/icons8-win10/angle-right.svg")' in style
    assert 'url("vendor/icons8-win10/share.svg")' in style
    assert 'url("vendor/icons8-win10/channels-sliders.svg")' in (STATIC / "native-v1.css").read_text(encoding="utf-8")


def test_disclosure_hides_the_whole_group_and_keeps_focus_accessible():
    result = run("""
      const attrs={},states=[];let focused=0;
      $('shell-tools-toggle').setAttribute=(name,value)=>attrs[name]=value;
      $('shell-tools-toggle').focus=()=>focused++;
      $('shell-tools').contains=()=>true;
      for(const expanded of [false,true,false]){
        setShellToolsExpanded(expanded);
        states.push({hidden:$('shell-tools').hidden,expanded:attrs['aria-expanded'],label:attrs['aria-label']});
      }
      ({states,focused,linked:compare.linked});
    """)
    assert result["states"] == [
        {"hidden": True, "expanded": "false", "label": "Show slide tools"},
        {"hidden": False, "expanded": "true", "label": "Hide slide tools"},
        {"hidden": True, "expanded": "false", "label": "Show slide tools"},
    ]
    assert result["focused"] == 2
    assert result["linked"] is True


CLOUD_SETUP = """
window.pywebview={api:{open_translucent_viewer(){}}};
compare.states.get('a').translucentAvailable=true;
compare.states.get('b').translucentAvailable=true;
"""


def test_cloud_and_info_route_to_the_active_source_not_the_compare_orientation_target():
    result = run(CLOUD_SETUP + """
      active='b';compare.orientationSid='c';
      openActiveTranslucent();panelShortcut('info');messages;
    """)
    assert [message["nd2wsi"] for message in result] == ["pane-translucent-open", "pane-panel-shortcut"]
    assert all(message["sid"] == "b" and message["paneInstanceId"] == "pane-b" and
               message["spatialContextKey"] == "context-b" for message in result)
    assert result[1]["panel"] == "info"


@pytest.mark.parametrize("setup", [
    "readyFrames.delete(active)", "compare.states.delete(active)",
    "compare.states.get(active).imageReady=false", "compare.states.get(active).plateGrid=true",
    "compare.states.get(active).translucentAvailable=false",
    "compare.states.get(active).translucentOpening=true", "quitPreparation={}",
    "pairPicker.open=true", "delete window.pywebview",
])
def test_cloud_ignores_unready_unsupported_busy_or_modal_sources(setup):
    result = run(CLOUD_SETUP + setup + "; ({accepted:openActiveTranslucent(),messages});")
    assert result == {"accepted": False, "messages": []}


def test_info_state_follows_active_pane_even_while_tools_are_collapsed():
    result = run("""
      compare.states.get('a').infoOpen=false;compare.states.get('b').infoOpen=true;
      setShellToolsExpanded(false);
      refreshShellToolButtons();const first=$('info-toggle').classList.contains('active');
      active='b';refreshShellToolButtons();
      ({first,second:$('info-toggle').classList.contains('active'),
        disabled:$('info-toggle').disabled,hidden:$('shell-tools').hidden});
    """)
    assert result == {"first": False, "second": True, "disabled": False, "hidden": True}


@pytest.mark.parametrize("setup,accepted", [
    ("", True), ("readyFrames.delete('b')", False), ("quitPreparation={}", False),
    ("data.paneInstanceId='stale'", False), ("data.contextEpoch=0", False),
    ("data.sid='a'", False), ("data.role='invalid'", False),
])
def test_windows_pane_new_window_request_validates_identity_before_shared_controller(setup, accepted):
    result = run("""
      const calls=[];
      window.Nd2WindowControls={canOpenWindow:()=>true,requestWindow:(doc,role)=>calls.push(role)};
      const data={sid:'b',role:'user',...localIdentity(compare.states.get('b'))};
    """ + setup + "; ({accepted:receiveNewWindowShortcut('b',data),calls});", platform="Win32")
    assert result == {"accepted": accepted, "calls": ["user"] if accepted else []}
