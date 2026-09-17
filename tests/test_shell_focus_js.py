"""The real shell hands keyboard focus to ready slides without stealing typing."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / 'nd2wsi' / 'static'
NODE = shutil.which('node')
pytestmark = pytest.mark.skipif(NODE is None, reason='node is not installed')

SCRIPT = r'''
const fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1]+'/shell-v1.js','utf8');
const ShortcutRouter=require(process.argv[1]+'/shortcut-router-v1.js');
const elements=new Map(),focused=[],messages=[];
const body={tagName:'BODY'},document={body,documentElement:{tagName:'HTML'},activeElement:body};
const frames=new Map(),readyFrames=new Set();
for (const sid of ['a','b']) {
  const frame={tagName:'IFRAME',dataset:{sid},contentWindow:{
    focus(){focused.push(sid+':window');},postMessage(data){messages.push(data);}},
    focus(){document.activeElement=frame;focused.push(sid);}};
  frames.set(sid,frame);
}
const context=vm.createContext({document,frames,readyFrames,ShortcutRouter,focused,messages,
  active:null,location:{origin:'http://qa.invalid'},quitPreparation:null,
  pairPicker:{open:false},compare:{enabled:false,members:['b']},
  closePairPicker(){},rememberSlide(){},ensureFrame(){},applyFrameLayout(){},render(){},
  scheduleNativeGestureScopes(){},inGroup:sid=>['a','b'].includes(sid),
  broadcastCompareState(){},spatialGroupReady:()=>false,
  sendTabShortcutState(){},groupSids:()=>['a','b'],setLinkMenuVisible(){},
  rememberAlignment(){},invalidateSpatialWork(){},clearDisplayTransforms(){},updateCompareControls(){},
});
for(const name of ['focusActivePane','activate','paneCameUp','stopCompare']) {
  const at=source.indexOf('function '+name+'(');
  vm.runInContext(source.slice(at,source.indexOf('\n}',at)+2),context);
}
process.stdout.write(JSON.stringify(vm.runInContext(process.argv[2],context)));
'''


def run(body):
    result = subprocess.run([NODE, '-e', SCRIPT, str(STATIC), body], capture_output=True,
                            text=True, check=True, timeout=20)
    return json.loads(result.stdout)


def test_initial_ready_pane_receives_focus_and_channel_key_can_reach_it():
    result = run('''
      activate('a'); const before=focused.slice();
      readyFrames.add('a'); paneCameUp('a');
      ({before,focused,target:document.activeElement.dataset.sid,
        action:ShortcutRouter.panelForEvent({key:'c',code:'KeyC',target:{tagName:'BODY'}})});
    ''')
    assert result == {'before': [], 'focused': ['a', 'a:window'], 'target': 'a', 'action': 'channels'}


def test_switching_to_ready_tab_focuses_it_once_without_resetting_inner_editor():
    result = run('''
      readyFrames.add('a');readyFrames.add('b');
      activate('a');activate('b');paneCameUp('b');
      ({focused,target:document.activeElement.dataset.sid});
    ''')
    assert result == {'focused': ['a', 'a:window', 'b', 'b:window'], 'target': 'b'}


@pytest.mark.parametrize('target', [
    "{tagName:'INPUT'}", "{tagName:'TEXTAREA'}", "{tagName:'SELECT'}",
    "{tagName:'DIV',isContentEditable:true}",
])
def test_activation_and_late_readiness_preserve_text_or_select_focus(target):
    result = run('''
      document.activeElement=TARGET;
      readyFrames.add('a');activate('a');paneCameUp('a');
      ({focused,tag:document.activeElement.tagName});
    '''.replace('TARGET', target))
    assert result['focused'] == []


def test_delayed_ready_does_not_take_focus_from_a_newly_selected_button():
    result = run('''
      activate('a');document.activeElement={tagName:'BUTTON'};
      readyFrames.add('a');paneCameUp('a');focused;
    ''')
    assert result == []


def test_inactive_ready_pane_does_not_take_focus_from_selected_slide():
    result = run('''
      readyFrames.add('a');activate('a');readyFrames.add('b');paneCameUp('b');
      ({focused,target:document.activeElement.dataset.sid});
    ''')
    assert result == {'focused': ['a', 'a:window'], 'target': 'a'}


def test_compare_keeps_visible_secondary_pane_focus_then_restores_after_stop():
    result = run('''
      active='a';readyFrames.add('a');readyFrames.add('b');compare.enabled=true;
      document.activeElement=frames.get('b');paneCameUp('a');const before=focused.slice();
      stopCompare();({before,focused,target:document.activeElement.dataset.sid});
    ''')
    assert result == {'before': [], 'focused': ['a', 'a:window'], 'target': 'a'}


def test_no_focus_change_during_quit_or_pair_picker():
    result = run('''
      active='a';readyFrames.add('a');quitPreparation={};focusActivePane();
      quitPreparation=null;pairPicker.open=true;focusActivePane();focused;
    ''')
    assert result == []
