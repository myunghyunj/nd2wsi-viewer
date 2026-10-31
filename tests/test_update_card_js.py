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
const doc={getElementById:element,documentElement:{dataset:{updateCardShell:'true'}},createElement:()=>element('button'+ ++uid)};
const win={setTimeout:(fn,ms)=>timers.push({fn,ms}),crypto:{randomUUID:()=>String(++uid)},addEventListener:(k,fn)=>listeners[k]=fn,
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
for(const [state,actions] of [['available',['later','download']],['downloading',['cancel']],['waiting',[]],['ready',['later','install']],['error',['close','retry']],['up_to_date',[]]]){
 show(state,actions,{on_quit:state==='ready'});labels[state]=buttons();
}
show('downloading',['cancel'],{received:55400000,total:132100000});
console.log(JSON.stringify({labels,detail:element('update-notice-detail').textContent,progress:element('update-notice-progress').value,
 text:element('update-notice-body').textContent,ready:card.ready}));
''')
    assert result["labels"] == {"available": ["Later", "Download"], "downloading": ["Cancel"], "waiting": [],
                                "ready": ["Install on quit", "Install and relaunch"], "error": ["Close", "Try again"], "up_to_date": []}
    assert result["detail"] == "55.4 MB of 132.1 MB"
    assert abs(result["progress"] - 55.4 / 132.1) < 1e-8
    assert result["text"] == "<img src=x onerror=bad()>" and result["ready"]
    css = (STATIC / "update-card-v1.css").read_text()
    assert css.count(".74)") == 2 and "blur(24px)" in css
    assert "#update-notice[hidden]" in css and ":root.light #update-notice" in css


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

    html = (STATIC / "shell.html").read_text()
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
