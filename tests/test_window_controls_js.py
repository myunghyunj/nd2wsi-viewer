import json
from pathlib import Path

import pytest
from node_host import run_node

MODULE = Path(__file__).resolve().parents[1] / "nd2wsi/static/window-controls-v1.js"


def run_case(role="agent", fail=False, supported=True):
    script = r"""
    const controls = require(process.argv[1]);
    const options = JSON.parse(process.argv[2]);
    const elements = new Map();
    const doc = {getElementById(id) {
      if (!elements.has(id)) elements.set(id, {hidden:true, dataset:{}, textContent:'',
        addEventListener(){}, focus(){}});
      return elements.get(id);
    }};
    const calls = [], errors = [];
    const api = {
      async window_context(){return {role:options.role, id:'abcdef123456', new_window_supported:options.supported};},
      async new_window(role){calls.push(role); return options.fail ? {ok:false,error:'launch failed'} : {ok:true,pid:123};}
    };
    (async () => {
      await controls.mount(doc, api, msg => errors.push(msg));
      if (options.supported) {
        await doc.getElementById('new-user-window').onclick();
        await doc.getElementById('new-agent-window').onclick();
      }
      process.stdout.write(JSON.stringify({calls, errors,
        hidden:doc.getElementById('window-menu').hidden,
        identity:doc.getElementById('window-menu').dataset,
        label:doc.getElementById('window-identity').textContent,
        status:doc.getElementById('window-action-status').textContent,
        disabled:doc.getElementById('new-agent-window').disabled}));
    })().catch(e => {console.error(e); process.exit(1);});
    """
    config = {"role": role, "fail": fail, "supported": supported}
    result = run_node(script, MODULE, json.dumps(config), check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize("role", ["user", "agent"])
def test_actions_create_distinct_roles_without_retargeting_existing_tabs(role):
    result = run_case(role)
    assert result["calls"] == ["user", "agent"]
    assert result["label"] == f"{role.title()} · abcdef"
    assert result["hidden"] is (role == "user")
    assert result["identity"] == {"role": role, "sessionId": "abcdef123456", "wired": "true"}
    assert result["disabled"] is False
    assert result["errors"] == []


def test_launch_errors_are_visible_and_controls_are_reenabled():
    result = run_case(fail=True)
    assert len(result["errors"]) == 2
    assert "launch failed" in result["status"]
    assert result["disabled"] is False


def test_unsupported_windows_and_browser_paths_keep_controls_hidden():
    result = run_case(supported=False)
    assert result["hidden"] is True
    assert result["calls"] == []


def shortcut_case(platform="Win32", *, boot=False, events=None, supported=True, hold=False):
    script = r"""
    const config=JSON.parse(process.argv[2]);
    Object.defineProperty(globalThis,'navigator',{value:{platform:config.platform},configurable:true});
    const controls=require(process.argv[1]);
    const router=require(require('path').join(require('path').dirname(process.argv[1]),'shortcut-router-v1.js'));
    const elements=new Map(),handlers=[],calls=[],seen=[];let reads=0,release;
    const win={Nd2ShortcutRouter:router,addEventListener(kind,fn){if(kind==='keydown')handlers.push(fn);}};
    const doc={defaultView:win,getElementById(id){
      if(config.boot && id!=='status')return null;
      if(!elements.has(id))elements.set(id,{dataset:{},hidden:true,textContent:'',disabled:false,
        addEventListener(){},focus(){}});return elements.get(id);}};
    const api={async window_context(){reads++;return {role:'user',id:'session123',new_window_supported:config.supported};},
      async new_window(role){calls.push(role);if(config.hold)await new Promise(resolve=>release=resolve);return {ok:true};}};
    (async()=>{
      await Promise.all([controls.mount(doc,api),controls.mount(doc,api)]);
      for(const spec of config.events){
        const event={key:'n',code:'KeyN',ctrlKey:true,metaKey:false,altKey:false,shiftKey:false,repeat:false,
          target:{tagName:'INPUT'},...spec,preventDefault(){this.defaultPrevented=true;},stopPropagation(){this.stopped=true;}};
        handlers.forEach(fn=>fn(event));seen.push({prevented:!!event.defaultPrevented,stopped:!!event.stopped});
        if(!config.hold){await Promise.resolve();await Promise.resolve();}
      }
      if(release){release();await Promise.resolve();await Promise.resolve();}
      process.stdout.write(JSON.stringify({reads,calls,seen,handlers:handlers.length,available:controls.canOpenWindow(doc)}));
    })().catch(error=>{console.error(error);process.exit(1);});
    """
    config = {
        "platform": platform, "boot": boot, "events": events or [{}], "supported": supported, "hold": hold,
    }
    result = run_node(script, MODULE, json.dumps(config), check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize("boot", [False, True])
def test_windows_new_window_runs_once_in_shell_or_empty_entry_even_in_text_field(boot):
    result = shortcut_case(boot=boot, events=[{}, {"repeat": True}, {"shiftKey": True}])
    assert result["calls"] == ["user", "agent"]
    assert result["reads"] == result["handlers"] == 1
    assert result["seen"] == [{"prevented": True, "stopped": True}] * 3


def test_new_window_launch_guard_covers_concurrent_shortcut_requests():
    result = shortcut_case(events=[{}, {}, {"shiftKey": True}], hold=True)
    assert result["calls"] == ["user"]


@pytest.mark.parametrize("event", [
    {"ctrlKey": False}, {"ctrlKey": False, "metaKey": True}, {"metaKey": True},
    {"altKey": True}, {"isComposing": True}, {"keyCode": 229}, {"defaultPrevented": True},
])
def test_windows_new_window_rejects_wrong_modifiers_and_composition(event):
    result = shortcut_case(events=[event])
    assert result["calls"] == []
    assert result["seen"][0]["stopped"] is False


def test_mac_new_window_remains_owned_by_native_menu_and_unsupported_context_stays_inert():
    result = shortcut_case(platform="MacIntel", events=[{"ctrlKey": False, "metaKey": True},
                                                         {"ctrlKey": False, "metaKey": True, "shiftKey": True}])
    assert result["handlers"] == 0 and result["calls"] == []
    unsupported = shortcut_case(boot=True, supported=False)
    assert unsupported["available"] is False and unsupported["handlers"] == 0


def test_empty_entry_embeds_the_same_shared_shortcut_and_window_controller():
    from nd2wsi.app import BOOT_HTML

    static = MODULE.parent
    assert (static / "shortcut-router-v1.js").read_text(encoding="utf-8") in BOOT_HTML
    assert MODULE.read_text(encoding="utf-8") in BOOT_HTML
