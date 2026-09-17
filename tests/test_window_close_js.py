"""Tab close must preserve edits before removing the production iframe.

Reuse the real shell/pane preparation harness; only HTTP transport and native
window availability are stubbed. No browser, source image or user window opens.
"""

import json
import subprocess

import pytest
from test_update_cancel_js import NODE, STATIC
from test_update_cancel_js import SCRIPT as PREPARATION_SCRIPT

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

CLOSE_BINDINGS = r"""
shell.slides = [{sid:'a'}, {sid:'b'}];
shell.closeRequests = [];
shell.closeResponse = {ok:true};
shell.closeTransportError = null;
shell.refreshCount = 0;
shell.fetch = async (url, options) => {
  shell.closeRequests.push({url, ...JSON.parse(options.body)});
  if (shell.closeTransportError) throw Error(shell.closeTransportError);
  const data = shell.closeResponse;
  return {ok:data.ok === true, status:data.status || 200, json:async()=>data};
};
shell.refresh = async () => {
  shell.refreshCount++;
  const sid = shell.closeRequests[shell.closeRequests.length - 1].sid;
  shell.slides = shell.slides.filter(slide => slide.sid !== sid);
  frames.delete(sid);
  shell.readyFrames.delete(sid);
};
vm.runInContext('let tabClosePending = false;', shell);
vm.runInContext(production(shellSource, 'closeTab'), shell);
"""

SCRIPT = PREPARATION_SCRIPT.replace(
    "resolve({ok:true, json:async()=>({path:'qa-only-annotations.json'})});",
    "resolve(pane.failSaves ? {ok:false, status:409, "
    "json:async()=>({error:'conflicting annotations', draft_path:pane.draftPath || null, draft_error:pane.draftError || null})} : "
    "{ok:true, json:async()=>({path:'qa-only-annotations.json'})});",
).replace("Promise.resolve(vm.runInContext", CLOSE_BINDINGS + "\nPromise.resolve(vm.runInContext", 1)


def run(body):
    result = subprocess.run(
        [NODE, "-e", SCRIPT, str(STATIC), body],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_close_waits_for_live_editor_save_before_removing_target_and_unlocks_survivor():
    out = run("""
      const a=panes.get('a'), b=panes.get('b');
      a.holdSaves=true; a.edit('keep this unsaved annotation');
      const pending=closeTab('a');
      await pump();
      const before={requests:closeRequests.length, frame:frames.has('a'),
        blocked:[a.inputBlocked(),b.inputBlocked()]};
      for (const save of a.saves.splice(0)) save();
      await pump();
      const closed=await pending;
      await pump();
      return {before, closed, requests:closeRequests, refreshCount,
        saved:a.writes[0].items[0].text, frames:[...frames.keys()],
        survivingBlocked:b.inputBlocked(), preparing:quitPreparationRequestId,
        busy:tabClosePending};
    """)
    assert out["before"] == {"requests": 0, "frame": True, "blocked": [True, True]}
    assert out["closed"] is True
    assert out["requests"] == [{"url": "api/close", "sid": "a"}]
    assert out["saved"] == "keep this unsaved annotation"
    assert out["frames"] == ["b"]
    assert out["refreshCount"] == 1
    assert out["survivingBlocked"] is False
    assert out["preparing"] == ""
    assert out["busy"] is False


def test_annotation_conflict_cancels_close_and_retains_retry_snapshot():
    out = run("""
      const a=panes.get('a');
      a.failSaves=true; a.edit('do not discard conflicted annotation');
      const pending=closeTab('a');
      await pump();
      const closed=await pending;
      await pump();
      return {closed, requests:closeRequests.length, frames:[...frames.keys()],
        dirty:a.state.annDirty, failed:a.state.annFailedSaves.size,
        text:a.state.annotations[0].text, blocked:a.inputBlocked(),
        preparing:quitPreparationRequestId, notices};
    """)
    assert out["closed"] is False
    assert out["requests"] == 0
    assert out["frames"] == ["a", "b"]
    assert out["dirty"] is True
    assert out["failed"] == 1
    assert out["text"] == "do not discard conflicted annotation"
    assert out["blocked"] is False
    assert out["preparing"] == ""
    assert "Close cancelled" in out["notices"][-1]


def test_timeout_preserves_frame_and_late_save_reply_cannot_close_it():
    out = run("""
      const a=panes.get('a'); a.holdSaves=true; a.edit('delayed annotation');
      const pending=closeTab('a');
      await pump(); runTimers(8000); await pump();
      const closed=await pending;
      for (const save of a.saves.splice(0)) save();
      await pump();
      return {closed, requests:closeRequests.length, frame:frames.has('a'),
        blocked:a.inputBlocked(), text:a.state.annotations[0].text, notices};
    """)
    assert out["closed"] is False
    assert out["requests"] == 0
    assert out["frame"] is True
    assert out["blocked"] is False
    assert out["text"] == "delayed annotation"
    assert "timed out" in out["notices"][-1]


@pytest.mark.parametrize("failure", ["http", "network"])
def test_server_export_refusal_or_network_failure_keeps_panes_editable(failure):
    setup = (
        "closeResponse={ok:false,status:409,error:'an export is still running'};"
        if failure == "http" else "closeTransportError='connection interrupted';"
    )
    out = run(setup + """
      const pending=closeTab('a'); await pump(); const closed=await pending; await pump();
      return {closed, frames:[...frames.keys()], refreshCount,
        blocked:[panes.get('a').inputBlocked(),panes.get('b').inputBlocked()],
        preparing:quitPreparationRequestId, busy:tabClosePending, notices};
    """)
    assert out["closed"] is False
    assert out["frames"] == ["a", "b"]
    assert out["refreshCount"] == 0
    assert out["blocked"] == [False, False]
    assert out["preparing"] == ""
    assert out["busy"] is False


def test_native_export_guard_blocks_before_any_preparation_or_close():
    out = run("""
      window.pywebview={api:{update_block_reason:async()=> 'Waiting for 1 export to finish…'}};
      const pending=closeTab('a'); await pump(); const closed=await pending;
      return {closed, requests:closeRequests.length, messages:messages.length,
        frames:[...frames.keys()], busy:tabClosePending, notices};
    """)
    assert out["closed"] is False
    assert out["requests"] == 0
    assert out["messages"] == 0
    assert out["frames"] == ["a", "b"]
    assert out["busy"] is False
    assert "export" in out["notices"][-1]


def test_repeated_close_cannot_remove_second_tab_or_double_submit():
    out = run("""
      const a=panes.get('a'); a.holdSaves=true; a.edit('held');
      const first=closeTab('a'); await pump();
      const repeated=await closeTab('a'); const other=await closeTab('b');
      for (const save of a.saves.splice(0)) save();
      await pump(); const closed=await first; await pump();
      return {closed, repeated, other, requests:closeRequests, frames:[...frames.keys()]};
    """)
    assert out["closed"] is True
    assert out["repeated"] is False
    assert out["other"] is False
    assert out["requests"] == [{"url": "api/close", "sid": "a"}]
    assert out["frames"] == ["b"]


def test_active_update_is_not_cancelled_or_restarted_by_tab_close():
    out = run("""
      const preparing=window.nd2wsiPrepareForUpdate('native-update');
      await pump(); await preparing;
      const closed=await closeTab('a'); await pump();
      return {closed, requests:closeRequests.length, preparing:quitPreparationRequestId,
        blocked:[panes.get('a').inputBlocked(),panes.get('b').inputBlocked()]};
    """)
    assert out["closed"] is False
    assert out["requests"] == 0
    assert out["preparing"] == "native-update"
    assert out["blocked"] == [True, True]


def test_superseding_preparation_cannot_be_unlocked_by_old_close():
    out = run("""
      const a=panes.get('a'); a.holdSaves=true; a.edit('held');
      const closing=closeTab('a'); await pump();
      const newer=window.nd2wsiPrepareForUpdate('native-close-newer');
      await pump(); const closed=await closing; await pump();
      return {closed, requests:closeRequests.length, preparing:quitPreparationRequestId,
        blocked:[a.inputBlocked(),panes.get('b').inputBlocked()]};
    """)
    assert out["closed"] is False
    assert out["requests"] == 0
    assert out["preparing"] == "native-close-newer"
    assert out["blocked"] == [True, True]


def test_loading_frame_is_not_removed_without_an_annotation_acknowledgement():
    out = run("""
      readyFrames.delete('a'); const closed=await closeTab('a');
      return {closed, requests:closeRequests.length, frame:frames.has('a'), notices};
    """)
    assert out["closed"] is False
    assert out["requests"] == 0
    assert out["frame"] is True
    assert "loading" in out["notices"][-1]


def test_verified_conflict_draft_requires_explicit_choice_before_close():
    out = run("""
      const a=panes.get('a');
      a.failSaves=true; a.draftPath='/session/drafts/recovery.json';
      a.edit('preserve my exact draft');
      const first=closeTab('a'); await pump(); const refused=await first; await pump();
      const notice=a.elements.get('ann-recovery');
      const before={refused,visible:!notice.hidden,requests:closeRequests.length};
      const accepted=a.acceptDrafts();
      const second=closeTab('a'); await pump(); const closed=await second; await pump();
      return {before,accepted,closed,requests:closeRequests.length,
        dirty:a.state.annDirty,failed:a.state.annFailedSaves.size,
        saved:a.writes.every(w=>w.items[0].text==='preserve my exact draft')};
    """)
    assert out["before"] == {"refused": False, "visible": True, "requests": 0}
    assert out["accepted"] and out["closed"] and out["saved"]
    assert out["requests"] == 1
    # Kept-for-recovery never means the source sidecar was successfully saved.
    assert out["dirty"] and out["failed"] == 1


@pytest.mark.parametrize("draft_error", ["null", "'disk full'"])
def test_missing_or_failed_draft_persistence_never_authorizes_close(draft_error):
    out = run("""
      const a=panes.get('a'); a.failSaves=true;
      a.draftPath=DRAFT_ERROR ? '/unconfirmed/path.json' : null; a.draftError=DRAFT_ERROR;
      a.edit('must remain open');
      const first=closeTab('a'); await pump(); await first; await pump();
      const accepted=a.acceptDrafts();
      const second=closeTab('a'); await pump(); const closed=await second; await pump();
      return {accepted,closed,requests:closeRequests.length,frame:frames.has('a'),
        failed:a.state.annFailedSaves.size};
    """.replace("DRAFT_ERROR", draft_error))
    assert out == {"accepted": False, "closed": False, "requests": 0, "frame": True, "failed": 1}


def test_new_editor_text_after_acceptance_requires_a_new_draft_choice():
    out = run("""
      const a=panes.get('a'); a.failSaves=true; a.draftPath='/session/old.json';
      a.edit('old draft'); const first=closeTab('a'); await pump(); await first; await pump();
      const accepted=a.acceptDrafts();
      a.edit('newer text not approved');
      const second=closeTab('a'); await pump(); const closed=await second; await pump();
      return {accepted,closed,requests:closeRequests.length,text:a.state.annotations[0].text,
        latest:a.writes[a.writes.length-1].items[0].text};
    """)
    assert out == {"accepted": True, "closed": False, "requests": 0,
                   "text": "newer text not approved", "latest": "newer text not approved"}


def test_live_editor_change_cannot_authorize_an_older_draft_before_flush():
    out = run("""
      const a=panes.get('a'); a.failSaves=true; a.draftPath='/session/old.json';
      a.edit('old draft'); const first=closeTab('a'); await pump(); await first; await pump();
      a.edit('text still in editor');
      const accepted=a.acceptDrafts();
      return {accepted,text:a.state.annotations[0].text,requests:closeRequests.length};
    """)
    assert out == {"accepted": False, "text": "text still in editor", "requests": 0}


def test_older_plate_site_without_a_durable_draft_blocks_close_of_accepted_current_site():
    out = run("""
      const a=panes.get('a'); a.failSaves=true; a.draftPath='/session/current.json';
      a.edit('current'); const first=closeTab('a'); await pump(); await first; await pump();
      const accepted=a.acceptDrafts();
      a.state.annFailedSaves.set('api/annotations?p=0',{
        url:'api/annotations?p=0',body:JSON.stringify({items:[{id:'old-site'}]}),
        revision:-1,context:-1,conflict:true,draftPath:null,draftBody:null});
      a.draftPath=null;
      const second=closeTab('a'); await pump(); const closed=await second; await pump();
      return {accepted,closed,requests:closeRequests.length,failed:a.state.annFailedSaves.size,
        retried:a.writes.some(w=>w.items[0].id==='old-site')};
    """)
    assert out == {"accepted": True, "closed": False, "requests": 0, "failed": 2, "retried": True}


def test_pending_older_plate_site_is_checked_after_the_current_draft_was_accepted():
    out = run("""
      const a=panes.get('a'); a.failSaves=true; a.draftPath='/session/current.json';
      a.edit('current'); const first=closeTab('a'); await pump(); await first; await pump();
      const accepted=a.acceptDrafts(); a.holdSaves=true; a.draftPath=null;
      a.api.queueAnnotationSave({url:'api/annotations?p=0',
        body:JSON.stringify({items:[{id:'queued-old-site'}]}),revision:-1,context:-1});
      await pump();
      const second=closeTab('a'); await pump();
      const before=closeRequests.length;
      a.holdSaves=false; for(const save of a.saves.splice(0)) save(); await pump();
      const closed=await second; await pump();
      return {accepted,before,closed,requests:closeRequests.length,failed:a.state.annFailedSaves.size};
    """)
    assert out == {"accepted": True, "before": 0, "closed": False, "requests": 0, "failed": 2}


def test_native_close_preparation_accepts_all_preserved_drafts_without_clearing_them():
    out = run("""
      const a=panes.get('a'); a.failSaves=true; a.draftPath='/session/current.json';
      a.edit('current'); const first=closeTab('a'); await pump(); await first; await pump();
      const body=JSON.stringify({items:[{id:'old-site'}]});
      a.state.annFailedSaves.set('api/annotations?p=0',{
        url:'api/annotations?p=0',body,revision:-1,context:-1,
        conflict:true,draftPath:'/session/old-site.json',draftBody:body});
      const accepted=a.acceptDrafts();
      const preparing=window.nd2wsiPrepareForUpdate('native-close'); await pump();
      const result=await preparing;
      return {accepted,result,failed:a.state.annFailedSaves.size,dirty:a.state.annDirty};
    """)
    assert out == {"accepted": True, "result": {"ok": True, "panes": 2}, "failed": 2, "dirty": True}


def test_any_new_annotation_edit_revokes_the_previous_close_choice():
    out = run("""
      const a=panes.get('a'); a.failSaves=true; a.draftPath='/session/preserved.json';
      a.edit('original'); const first=closeTab('a'); await pump(); await first; await pump();
      a.acceptDrafts();
      a.api.annotationsChanged();
      const second=closeTab('a'); await pump(); const closed=await second; await pump();
      return {closed,requests:closeRequests.length,approved:a.state.annAcceptedDrafts.size};
    """)
    assert out == {"closed": False, "requests": 0, "approved": 0}
