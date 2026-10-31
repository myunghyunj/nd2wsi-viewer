"""The actual update card renders every state without treating notes as HTML."""
import json
from pathlib import Path

from node_host import run_node

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi/static"
SETUP = r'''
const assert=require('assert'),elements=new Map(),calls=[],listeners={},timers=[];
function element(id){if(!elements.has(id))elements.set(id,{id,hidden:true,children:[],dataset:{},textContent:'',
 set innerHTML(v){throw Error('HTML is forbidden');},replaceChildren(){this.children=[];},
 appendChild(c){this.children.push(c);},addEventListener(k,fn){this[k]=fn;},removeAttribute(k){delete this[k];}});return elements.get(id);}
let uid=0;
const doc={getElementById:element,documentElement:{dataset:{updateCardShell:'true'}},createElement:tag=>Object.assign(element('node'+ ++uid),{tagName:tag})};
const win={setTimeout:(fn,ms)=>timers.push({fn,ms}),crypto:{getRandomValues:require('crypto').webcrypto.getRandomValues.bind(require('crypto').webcrypto)},addEventListener:(k,fn)=>listeners[k]=fn,
 pywebview:{api:{update_card_action:async p=>{calls.push(p);return {ok:true};},update_card_closed:async t=>calls.push(t),
 check_for_updates:async flag=>{calls.push(flag);return {ok:true,available:true,mode:'manual-download',token:'manual',version:'2.5.6',message:'Close all windows before installing.'};},
 open_update_download:async token=>{calls.push(token);return {ok:true};},open_update_notes:async token=>{calls.push('notes:'+token);return {ok:true};}}}};
const card=require(process.argv[1]+'/update-card-v1.js').mount(win,doc);
const buttons=()=>element('update-notice-actions').children.map(b=>b.textContent);
const show=(state,actions,extra={})=>card.show({state,actions,token:'session',prompt:'prompt',title:'Title',message:'<img src=x onerror=bad()>',...extra});
'''


def run(body):
    r = run_node(SETUP + '(async()=>{' + body + '})().catch(e=>{console.error(e);process.exit(1)});', str(STATIC))
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_states_progress_labels_translucency_and_plain_text():
    result = run('''
const labels={};
for(const [state,actions] of [['available',['later','download']],['downloading',['cancel']],['waiting',[]],['ready',['install','cancel']],['error',['close','retry']],['up_to_date',[]]]){
 show(state,actions);labels[state]=buttons();
}
show('downloading',['cancel'],{received:55400000,total:132100000});
console.log(JSON.stringify({labels,detail:element('update-notice-detail').textContent,progress:element('update-notice-progress').value,
 text:element('update-notice-body').textContent,ready:card.ready}));
''')
    assert result["labels"] == {"available": ["Later", "Download"], "downloading": ["Cancel"], "waiting": [],
                                "ready": ["Install and relaunch", "Cancel"], "error": ["Close", "Try again"], "up_to_date": []}
    assert result["detail"] == "55.4 MB of 132.1 MB"
    assert abs(result["progress"] - 55.4 / 132.1) < 1e-8
    assert result["text"] == "<img src=x onerror=bad()>" and result["ready"]
    css = (STATIC / "update-card-v1.css").read_text(encoding="utf-8")
    assert "background: var(--material-popover)" in css
    assert "backdrop-filter: var(--material-popover-filter)" in css
    assert "#update-notice[hidden]" in css


def test_buttons_keep_prompt_identity_and_notice_reuses_card():
    result = run('''
show('available',['download']);await element('update-notice-actions').children[0].click();
card.notice('Waiting for 1 export to finish…');const waiting={buttons:buttons(),body:element('update-notice-body').textContent};
listeners.pagehide();assert.equal(card.dismiss('wrong'),false);card.dismiss('session');
console.log(JSON.stringify({calls,waiting,hidden:element('update-notice').hidden}));
''')
    assert result["calls"] == [{"token": "session", "prompt": "prompt", "action": "download"}, "session"]
    assert result["waiting"] == {"buttons": [], "body": "Waiting for 1 export to finish…"}
    assert result["hidden"]


def test_windows_manual_check_does_not_open_download_until_clicked():
    result = run('''
await card.check();const before=calls.slice(),labels=buttons();await element('update-notice-actions').children[1].click();
doc.documentElement.dataset.updateCardShell='';const landing=require(process.argv[1]+'/update-card-v1.js').mount(win,doc);
console.log(JSON.stringify({before,calls,labels,landingReady:landing.ready}));
''')
    assert result == {"before": [False], "calls": [False, "manual"], "labels": ["Later", "Open download"], "landingReady": False}


def test_shell_markup_precedes_card_script_and_boot_reuses_same_module():
    from nd2wsi.app import BOOT_HTML

    html = (STATIC / "shell.html").read_text(encoding="utf-8")
    assert html.index('id="update-notice"') < html.index('src="static/update-card-v1.js"') < html.index('src="static/shell-v1.js"')
    assert 'data-update-card-shell="true"' in html
    assert 'data-update-card-shell="true"' not in BOOT_HTML
    assert 'window.nd2wsiUpdateCard.check()' in BOOT_HTML


def test_manual_up_to_date_has_no_actions_and_timeout_cannot_hide_a_new_offer():
    result = run('''
card.manual({ok:true,available:false,message:"You're up to date."});
assert.deepEqual(buttons(),[]);assert.equal(timers[0].ms,5000);
show('available',['later','download']);timers[0].fn();
assert.equal(element('update-notice').hidden,false);
card.manual({ok:true,available:false});timers[1].fn();
console.log(JSON.stringify({hidden:element('update-notice').hidden}));
''')
    assert result == {"hidden": True}


def test_landing_ids_work_without_random_uuid():
    result = run('''
assert.equal(win.crypto.randomUUID,undefined);
doc.documentElement.dataset.updateCardShell='';
const landing=require(process.argv[1]+'/update-card-v1.js').mount(win,doc);
await landing.check();
console.log(JSON.stringify({id:landing.pageId,ready:landing.ready,calls,labels:buttons()}));
''')
    assert len(result["id"]) == 32 and int(result["id"], 16) >= 0
    assert not result["ready"] and result["calls"] == [False]
    assert result["labels"] == ["Later", "Open download"]


def test_landing_button_falls_back_without_card_and_restores_itself_after_sync_failure():
    from nd2wsi.app import BOOT_HTML

    start = BOOT_HTML.index("  document.getElementById('update').addEventListener('click'")
    listener = BOOT_HTML[start:BOOT_HTML.index("\n  });", start) + len("\n  });")]
    result = run("const listener=" + json.dumps(listener) + """
const statuses=[],fallback=[];
const ctx={document:doc,window:{},pywebview:{api:{check_for_updates(){fallback.push(true);return Promise.resolve({ok:true});}}},setStatus:t=>statuses.push(t)};
require('vm').runInNewContext(listener,ctx);
const button=element('update');button.click({currentTarget:button});
for(let i=0;i<10;i++)await Promise.resolve();
assert.equal(button.disabled,false);
ctx.pywebview.api.check_for_updates=()=>{throw Error('offline');};
button.click({currentTarget:button});for(let i=0;i<10;i++)await Promise.resolve();
console.log(JSON.stringify({fallback,disabled:button.disabled,statuses}));
""")
    assert result["fallback"] == [True] and not result["disabled"]
    assert "offline" in result["statuses"][0]


def test_answered_actions_stay_removed_and_focus_preserves_export_wait_notice():
    result = run('''
show('ready',['install','cancel']);await element('update-notice-actions').children[0].click();
const afterClick=buttons();show('ready',['install','cancel']);const afterFocus=buttons();
card.notice('Waiting for 1 export to finish…');show('ready',['install','cancel']);
const notice=element('update-notice-body').textContent;
card.show({token:'session',prompt:'new-prompt',state:'error',title:'Error',message:'A new failure',actions:['close','retry']});
console.log(JSON.stringify({afterClick,afterFocus,notice,next:buttons(),message:element('update-notice-body').textContent}));
''')
    assert result == {"afterClick": [], "afterFocus": [], "notice": "Waiting for 1 export to finish…",
                      "next": ["Close", "Try again"], "message": "A new failure"}


def test_focus_during_action_preserves_busy_state_and_retires_answered_buttons():
    result = run('''
let resolve;
win.pywebview.api.update_card_action=()=>new Promise(r=>{resolve=r;});
show('ready',['install','cancel']);
const pending=element('update-notice-actions').children[0].click();
show('ready',['install','cancel']);
const during=element('update-notice-actions').children.map(b=>b.disabled);
resolve({ok:true});await pending;
const after=buttons();
show('ready',['install'],{prompt:'second'});
const pending2=element('update-notice-actions').children[0].click();
show('error',['close','retry'],{prompt:'third'});
resolve({ok:true});await pending2;
console.log(JSON.stringify({during,after,next:buttons(),disabled:element('update-notice-actions').children.map(b=>b.disabled)}));
''')
    assert result == {"during": [True, True], "after": [], "next": ["Close", "Try again"], "disabled": [False, False]}


def test_release_notes_are_bounded_text_bullets_and_do_not_rebuild_on_progress():
    result = run('''
const release_notes=['  Clearer panels  ','<img src=x onerror=bad()>',null,{},' ',...Array(12).fill('x'.repeat(500))];
show('available',['later','download'],{release_notes});
const list=element('update-notice-changes'),first=list.children[0];
const items=list.children.map(n=>({tag:n.tagName,text:n.textContent}));
show('downloading',['cancel'],{release_notes,received:50,total:100});
const unchanged=list.children[0]===first,visible=!list.hidden;
show('ready',['install','cancel'],{release_notes,message:''});
const ready=list.children.map(n=>n.textContent),bodyHidden=element('update-notice-body').hidden;
show('available',['later','download'],{prompt:'new-release',release_notes:[]});
console.log(JSON.stringify({items,unchanged,visible,ready,bodyHidden,empty:list.hidden,count:list.children.length}));
''')
    assert len(result["items"]) == 8
    assert result["items"][:2] == [
        {"tag": "li", "text": "Clearer panels"},
        {"tag": "li", "text": "<img src=x onerror=bad()>"},
    ]
    assert all(item == {"tag": "li", "text": "x" * 300} for item in result["items"][2:])
    assert result["unchanged"] and result["visible"] and result["bodyHidden"]
    assert result["ready"] == [item["text"] for item in result["items"]]
    assert result["empty"] and result["count"] == 0


def test_ready_install_is_first_primary_and_cancel_is_a_separate_neutral_action():
    result = run('''
show('ready',['cancel','install'],{release_notes:['A real update'],message:''});
const nodes=element('update-notice-actions').children;
const layout=nodes.map(n=>({label:n.textContent,primary:n.className==='update-primary',action:n.dataset.action}));
await nodes[1].click();
console.log(JSON.stringify({layout,calls,remaining:buttons(),state:element('update-notice').dataset.state}));
''')
    assert result["layout"] == [
        {"label": "Install and relaunch", "primary": True, "action": "install"},
        {"label": "Cancel", "primary": False, "action": "cancel"},
    ]
    assert result["calls"] == [{"token": "session", "prompt": "prompt", "action": "cancel"}]
    assert result["remaining"] == [] and result["state"] == "ready"


def test_manual_release_notes_reach_the_same_bullet_list_and_missing_notes_stay_empty():
    result = run('''
card.manual({ok:true,available:true,version:'2.5.7',token:'next',release_notes:['Next version change'],message:'Close the viewer before installing.'});
const list=element('update-notice-changes');
const first={items:list.children.map(n=>n.textContent),hidden:list.hidden,notes:element('update-notice-notes').hidden};
card.manual({ok:true,available:true,version:'2.5.8',token:'later'});
console.log(JSON.stringify({first,missing:list.children.map(n=>n.textContent),hidden:list.hidden}));
''')
    assert result == {"first": {"items": ["Next version change"], "hidden": False, "notes": False},
                      "missing": [], "hidden": True}


def test_failed_install_shows_status_but_a_late_failure_cannot_replace_new_notes():
    result = run('''
win.pywebview.api.update_card_action=async()=>{throw Error('Unable to save');};
show('ready',['install','cancel'],{release_notes:['First change'],message:''});
await element('update-notice-actions').children[0].click();
const failure={body:element('update-notice-body').textContent,hidden:element('update-notice-body').hidden};
let reject;
win.pywebview.api.update_card_action=()=>new Promise((_,r)=>{reject=r;});
show('ready',['install','cancel'],{prompt:'old',release_notes:['Old change'],message:''});
const pending=element('update-notice-actions').children[0].click();
show('ready',['install','cancel'],{token:'new',prompt:'new',release_notes:['New change'],message:''});
reject(Error('Old error'));await pending;
console.log(JSON.stringify({failure,items:element('update-notice-changes').children.map(n=>n.textContent),
 body:element('update-notice-body').textContent,hidden:element('update-notice-body').hidden,buttons:buttons()}));
''')
    assert result == {"failure": {"body": "Unable to save", "hidden": False}, "items": ["New change"],
                      "body": "", "hidden": True, "buttons": ["Install and relaunch", "Cancel"]}
