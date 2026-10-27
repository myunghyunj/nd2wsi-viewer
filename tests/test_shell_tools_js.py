"""Top slide tools share a disclosure and route only to the current source pane."""

import json
import subprocess
from html.parser import HTMLParser

import pytest
from test_compare_orientation_js import NODE, SCRIPT, STATIC, run

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


def run_slacs(body):
    """Reuse the shell harness, including the real discovery state and lifecycle bindings."""
    script = SCRIPT.replace(
        "const out = vm.runInContext(process.argv[2], context);\nprocess.stdout.write(JSON.stringify(out));",
        r"""
const discoveryStart=source.indexOf('const slacsDiscovery = {');
vm.runInContext(source.slice(discoveryStart,source.indexOf('\n};',discoveryStart)+3),context);
vm.runInContext('bindSlacsDiscovery()',context);
context.lifecycle=kind=>Promise.all((listeners.get(kind)||[]).map(fn=>fn()));
context.discoveryTick=async(delay=1000)=>{
  for(const [id,timer] of [...timers]) if(timer.delay===delay && timers.has(id)) {
    timers.delete(id);await timer.fn();
  }
};
Promise.resolve(vm.runInContext(process.argv[2],context))
  .then(out=>process.stdout.write(JSON.stringify(out)))
  .catch(error=>{console.error(error);process.exitCode=1;});
""",
    )
    result = subprocess.run(
        [NODE, "-e", script, str(STATIC), body, "Win32"],
        capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


DETECTED = "{available:true,status:'running',session_id:'sniper-1',offer:true}"


def test_slacs_offer_is_initially_hidden_nonmodal_and_does_not_gate_normal_translucent():
    shell = Elements((STATIC / "shell.html").read_text(encoding="utf-8")).elements
    assert "hidden" in shell["slacs-offer"][0]
    assert shell["slacs-offer"][0]["aria-labelledby"] == "slacs-offer-title"
    assert "aria-modal" not in shell["slacs-offer"][0]
    assert "disabled" in shell["slacs-offer-open"][0]
    assert "#slacs-offer[hidden] { display:none; }" in (STATIC / "native-shell-v1.css").read_text()
    result = run_slacs(CLOUD_SETUP + """
      ({hidden:$('slacs-offer').hidden,running:slacsDiscovery.running,
        timers:getTimerCallbacks().length,accepted:openActiveTranslucent(),messages});
    """)
    assert result["hidden"] is True
    assert result["running"] is False
    assert result["timers"] == 0
    assert result["accepted"]
    assert result["messages"][0]["nd2wsi"] == "pane-translucent-open"


@pytest.mark.parametrize("snapshot", [
    "null", "{}", "{available:false,status:'running',session_id:'a'}",
    "{available:true,status:'error',session_id:'a'}",
    "{available:true,status:'running',session_id:null}",
    "{available:true,status:'running',session_id:' '}",
    "{available:true,status:'running',session_id:'a'}",
    "{available:true,status:'running',session_id:'a',offer:false}",
])
def test_slacs_offer_requires_a_confirmed_running_session(snapshot):
    result = run_slacs(CLOUD_SETUP + f"""
      receiveSlacsDiscovery({snapshot});
      ({{hidden:$('slacs-offer').hidden,opened:openSlacsOffer(),messages}});
    """)
    assert result == {"hidden": True, "opened": False, "messages": []}


def test_slacs_offer_appears_once_per_session_without_starting_or_stealing_focus():
    result = run_slacs(CLOUD_SETUP + """
      const focus={tagName:'INPUT'};document.activeElement=focus;
      const states=[];
      const detect=value=>{receiveSlacsDiscovery(value);states.push($('slacs-offer').hidden);};
      detect(DETECTED);detect(DETECTED);
      $('slacs-offer-dismiss').onclick();states.push($('slacs-offer').hidden);
      detect(DETECTED);detect(null);detect(DETECTED);
      detect({...DETECTED,session_id:'sniper-2'});detect(null);
      ({states,messages,sameFocus:document.activeElement===focus});
    """.replace("DETECTED", DETECTED))
    assert result == {"states": [False, False, True, True, True, True, False, True],
                      "messages": [], "sameFocus": True}


def test_slacs_offer_guides_missing_image_then_explicitly_opens_current_eligible_pane():
    result = run_slacs(CLOUD_SETUP + """
      active='b';compare.states.get('b').translucentAvailable=false;
      receiveSlacsDiscovery(DETECTED);
      const unavailable={disabled:$('slacs-offer-open').disabled,
        hint:$('slacs-offer-message').textContent,opened:openSlacsOffer()};
      compare.states.get('b').translucentAvailable=true;
      receiveSlacsDiscovery(DETECTED);
      const before=messages.length;
      const accepted=$('slacs-offer-open').onclick();
      receiveSlacsDiscovery(DETECTED);
      ({unavailable,before,accepted,messages,hidden:$('slacs-offer').hidden});
    """.replace("DETECTED", DETECTED))
    assert result["unavailable"] == {"disabled": True, "hint":
                                    "Open a fluorescence image with +, then choose Open SLACS view.",
                                    "opened": False}
    assert result["before"] == 0
    assert result["accepted"] is True
    assert result["hidden"] is True
    assert len(result["messages"]) == 1
    assert result["messages"][0]["nd2wsi"] == "pane-translucent-open"
    assert result["messages"][0]["sid"] == "b"
    assert result["messages"][0]["paneInstanceId"] == "pane-b"


def test_main_discovery_runs_without_an_overlay_or_slide_and_retries_after_failure():
    result = run_slacs("""
      (async()=>{
        frames.clear();readyFrames.clear();compare.states.clear();active=null;
        let calls=0,failed=false;
        window.pywebview={api:{slacs_discovery_state(){calls++;if(failed)throw Error('unavailable');
          return DETECTED;},open_translucent_viewer(){throw Error('must stay opt in');}}};
        await lifecycle('pywebviewready');
        const detected={hidden:$('slacs-offer').hidden,disabled:$('slacs-offer-open').disabled};
        await lifecycle('pywebviewready');
        const single={calls,timers:getTimerCallbacks().length};
        failed=true;await discoveryTick();
        const lost={hidden:$('slacs-offer').hidden,calls,timers:getTimerCallbacks().length};
        failed=false;await discoveryTick();
        return {detected,single,lost,sameSessionHidden:$('slacs-offer').hidden,messages};
      })();
    """.replace("DETECTED", DETECTED))
    assert result == {"detected": {"hidden": False, "disabled": True},
                      "single": {"calls": 1, "timers": 1},
                      "lost": {"hidden": True, "calls": 2, "timers": 1},
                      "sameSessionHidden": True, "messages": []}


def test_main_discovery_ignores_late_results_after_pagehide_and_cancels_polling():
    result = run_slacs(CLOUD_SETUP + """
      (async()=>{
        let reply;
        window.pywebview.api.slacs_discovery_state=()=>new Promise(resolve=>{reply=resolve;});
        const pending=lifecycle('pywebviewready');
        await Promise.resolve();await lifecycle('pagehide');reply(DETECTED);await pending;
        const stopped={hidden:$('slacs-offer').hidden,running:slacsDiscovery.running,
          timers:getTimerCallbacks().length};
        window.pywebview.api.slacs_discovery_state=()=>(DETECTED);
        await lifecycle('pageshow');
        const resumed={hidden:$('slacs-offer').hidden,timers:getTimerCallbacks().length};
        await lifecycle('pagehide');
        return {stopped,resumed,finalTimers:getTimerCallbacks().length,messages};
      })();
    """.replace("DETECTED", DETECTED))
    assert result == {"stopped": {"hidden": True, "running": False, "timers": 0},
                      "resumed": {"hidden": False, "timers": 1}, "finalTimers": 0, "messages": []}


def test_main_discovery_timeout_hides_offer_retries_and_ignores_late_bridge_reply():
    result = run_slacs(CLOUD_SETUP + """
      (async()=>{
        window.pywebview.api.slacs_discovery_state=()=>(DETECTED);
        await lifecycle('pywebviewready');
        const initiallyHidden=$('slacs-offer').hidden;
        let reply;
        window.pywebview.api.slacs_discovery_state=()=>new Promise(resolve=>{reply=resolve;});
        const pending=discoveryTick();await Promise.resolve();
        await discoveryTick(3000);await pending;
        const timedOut={hidden:$('slacs-offer').hidden,timers:getTimerCallbacks().length};
        window.pywebview.api.slacs_discovery_state=()=>({...DETECTED,session_id:'sniper-2'});
        await discoveryTick();
        reply(DETECTED);await Promise.resolve();await Promise.resolve();
        return {initiallyHidden,timedOut,hidden:$('slacs-offer').hidden,
          session:slacsDiscovery.snapshot.session_id,timers:getTimerCallbacks().length,messages};
      })();
    """.replace("DETECTED", DETECTED))
    assert result == {"initiallyHidden": False, "timedOut": {"hidden": True, "timers": 1},
                      "hidden": False, "session": "sniper-2", "timers": 1, "messages": []}


def _offer_windows(tmp_path, monkeypatch, sessions):
    """Real Api objects for separate viewer windows, sharing one session folder."""
    from unittest.mock import Mock

    from nd2wsi import slacs_discovery
    from nd2wsi.app import Api
    from nd2wsi.window_sessions import create_window_session

    detected = {"available": True, "status": "running", "session_id": "slacs-" + "a" * 32}
    detector = Mock()
    detector.snapshot.side_effect = lambda: dict(detected)
    monkeypatch.setattr(slacs_discovery, "SlacsDiscovery", Mock(return_value=detector))
    windows = []
    for role in sessions:
        session = create_window_session(role, tmp_path / "sessions")
        windows.append((session, Api(None, window_session=session)))
    return detected, windows


def _shell_window(snapshots):
    """One independent shell process: feed each backend snapshot, record visibility."""
    return run_slacs(CLOUD_SETUP + """
      const visible=[];
      for(const snapshot of SNAPSHOTS){
        if(snapshot==='dismiss')$('slacs-offer-dismiss').onclick();else receiveSlacsDiscovery(snapshot);
        visible.push(!$('slacs-offer').hidden);
      }
      ({visible,messages});
    """.replace("SNAPSHOTS", json.dumps(snapshots)))


def test_slacs_offer_appears_once_across_independent_viewer_windows(tmp_path, monkeypatch):
    detected, windows = _offer_windows(tmp_path, monkeypatch, ["user", "user", "agent"])
    (_, first), (_, second), (_, agent) = windows
    try:
        background = second.slacs_discovery_state(False)  # polled first, but nobody is looking at it
        first_seen = [first.slacs_discovery_state(True) for _ in range(2)]  # the focused window
        second_seen = [second.slacs_discovery_state(True), second.slacs_discovery_state(False)]
        agent_seen = agent.slacs_discovery_state(True)
        detected["session_id"] = "slacs-" + "b" * 32  # SLACS restarted: a new process identity
        restarted = [second.slacs_discovery_state(True), first.slacs_discovery_state(True)]
        later = _late_window(tmp_path)  # a viewer window opened after the claim
        reopened = later.slacs_discovery_state(True)
    finally:
        for session, _ in windows:
            session.mark_closed()
    assert background["offer"] is False
    assert [s["offer"] for s in first_seen] == [True, True]
    assert [s["offer"] for s in second_seen] == [False, False]
    assert agent_seen["offer"] is True  # Agent automation never consumes the User offer
    assert [s["offer"] for s in restarted] == [True, False]
    assert reopened["offer"] is False
    one = _shell_window([first_seen[0], first_seen[1], "dismiss", first_seen[1], restarted[1]])
    two = _shell_window([background, second_seen[0], second_seen[1], restarted[0], restarted[0]])
    assert one == {"visible": [True, True, False, False, False], "messages": []}
    assert two == {"visible": [False, False, False, True, True], "messages": []}


def _late_window(tmp_path):
    from nd2wsi.app import Api
    from nd2wsi.window_sessions import create_window_session

    session = create_window_session("user", tmp_path / "sessions")
    session.mark_closed()  # releases the lifetime lock; claims do not depend on it
    return Api(None, window_session=session)


@pytest.mark.parametrize("visibility,focused,expected", [
    ("visible", True, True), ("visible", False, False), ("hidden", True, False)])
def test_shell_claims_the_offer_only_while_the_user_is_looking_at_it(visibility, focused, expected):
    result = run_slacs(f"""
      (async()=>{{
        const asked=[];
        document.visibilityState='{visibility}';document.hasFocus=()=>{str(focused).lower()};
        window.pywebview={{api:{{slacs_discovery_state(claim){{asked.push(claim);return DETECTED;}}}}}};
        await lifecycle('pywebviewready');
        return {{asked,hidden:$('slacs-offer').hidden}};
      }})();
    """.replace("DETECTED", DETECTED))
    assert result["asked"] == [expected]


def test_background_window_neither_claims_nor_caches_and_a_later_focus_claims(tmp_path, monkeypatch):
    detected, windows = _offer_windows(tmp_path, monkeypatch, ["user", "user"])
    (_, first), (_, second) = windows
    folder = tmp_path / "sessions" / "slacs-offers" / "user"
    try:
        unfocused = [first.slacs_discovery_state(False) for _ in range(3)]
        claimed_nothing = not folder.exists() or not any(folder.iterdir())
        focused = first.slacs_discovery_state(True)
        for claim in folder.iterdir():  # the claim file vanishes; each window keeps its answer
            claim.unlink()
        cached = [first.slacs_discovery_state(True), second.slacs_discovery_state(True)]
        refused = second.slacs_discovery_state(True)
    finally:
        for session, _ in windows:
            session.mark_closed()
    assert [s["offer"] for s in unfocused] == [False, False, False] and claimed_nothing
    assert focused["offer"] is True
    assert [s["offer"] for s in cached] == [True, True]  # second claims afresh: the file was gone
    assert refused["offer"] is True  # and keeps that answer from its own cache
