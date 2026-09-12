import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
MODULE = Path(__file__).resolve().parents[1] / "nd2wsi/static/window-controls-v1.js"
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


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
        label:doc.getElementById('window-identity').textContent,
        status:doc.getElementById('window-action-status').textContent,
        disabled:doc.getElementById('new-agent-window').disabled}));
    })().catch(e => {console.error(e); process.exit(1);});
    """
    result = subprocess.run([NODE, "-e", script, str(MODULE), json.dumps({"role": role, "fail": fail, "supported": supported})], capture_output=True, text=True, check=True, timeout=20)
    return json.loads(result.stdout)


@pytest.mark.parametrize("role", ["user", "agent"])
def test_actions_create_distinct_roles_without_retargeting_existing_tabs(role):
    result = run_case(role)
    assert result["calls"] == ["user", "agent"]
    assert result["label"] == f"{role.title()} · abcdef"
    assert result["hidden"] is False
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
