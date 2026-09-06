"""Delayed-message and atomic-edit regressions against the production shell.

The shared Node VM harness executes real shell transactions and supplies only
browser/DOM boundaries. In particular, replies use receiveViewportState, not a
shortcut to a transaction completion callback with unvalidated snapshots.
"""

import pytest
from test_compare_orientation_js import NODE, run

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

NUDGE_REPLY = r"""
function replyNudge(delta, changes={}) {
  const pending=compare.pendingNudge;
  const member=compare.states.get(pending.sid);
  return receiveViewportState({...member,...spatialEnvelope(pending.sid),version:2,
    seq:member.seq+1,reason:'user',nudge:true,nudgeCommandId:pending.commandId,
    commandSeq:pending.commandSeq,committedRevision:pending.committedRevision,
    nudgeDeltaPx:delta,...changes},pending.sid);
}
"""


def test_unknown_or_cancelled_request_reply_never_updates_global_viewport():
    result = run("""
      const before = JSON.stringify(compare.states.get('b'));
      receiveViewportState({...compare.states.get('b'),...spatialEnvelope('b'),version:2,
        requestId:'unknown-cancelled-request',seq:9999,
        centerPx:{x:98765,y:43210}},'b');
      ({unchanged:JSON.stringify(compare.states.get('b'))===before,
        request:compare.pendingRequest});
    """)
    assert result == {"unchanged": True, "request": None}


def test_snapshot_replies_remain_private_until_every_expected_pane_replies():
    result = run("""
      requestGroup('capture');
      const pending = compare.pendingRequest;
      const before = JSON.stringify(compare.states.get('b'));
      receiveViewportState({...compare.states.get('b'),...spatialEnvelope('b'),version:2,
        requestId:pending.requestId,groupSessionId:pending.groupSessionId,
        groupEpoch:pending.groupEpoch,seq:99,reason:'request',
        centerPx:{x:111,y:222}},'b');
      ({unchanged:JSON.stringify(compare.states.get('b'))===before,
        active:compare.pendingRequest===pending,
        privateReply:pending.responses.get('b').centerPx});
    """)
    assert result == {"unchanged": True, "active": True, "privateReply": {"x": 111, "y": 222}}


def test_old_timeout_callback_cannot_clear_next_transaction():
    result = run("""
      requestGroup('capture');
      const oldCallbacks = getTimerCallbacks();
      clearPendingRequest();
      requestGroup('capture');
      const current = compare.pendingRequest;
      for(const callback of oldCallbacks) callback();
      ({same:compare.pendingRequest===current,
        responses:current.responses.size});
    """)
    assert result == {"same": True, "responses": 0}


@pytest.mark.parametrize("operation", [
    "changeOrientation('transpose')",
    "nudgeAlignment(1,0,'a')",
    "startLandmarks()",
    "requestGroup('capture')",
    "recaptureAll()",
    "forwardViewport('a',compare.states.get('a'))",
])
def test_one_grid_pane_blocks_all_group_spatial_operations(operation):
    result = run(f"""
      compare.states.get('c').plateGrid=true;
      compare.states.get('c').spatialContext=null;
      const before=JSON.stringify([...compare.pairs]);
      {operation};
      ({{unchanged:JSON.stringify([...compare.pairs])===before,
        editing:compare.landmark.active,
        spatialCommands:messages.filter(m=>[
          'viewport-request','viewport-apply','viewport-nudge','display-transform'
        ].includes(m.nd2wsi)).length}});
    """)
    assert result == {"unchanged": True, "editing": False, "spatialCommands": 0}


def test_anchor_nudge_uses_active_member_and_member_nudge_uses_its_own_pane():
    result = run("""
      compare.orientationSid='c';
      nudgeAlignment(1,0,'a');
      const anchorTargets=messages.filter(m=>m.nd2wsi==='viewport-nudge').map(m=>m.sid);
      clearNudgeCommands(); // next independent keyboard interaction
      messages.length=0;
      nudgeAlignment(0,1,'b');
      const memberTargets=messages.filter(m=>m.nd2wsi==='viewport-nudge').map(m=>m.sid);
      ({anchorTargets,memberTargets});
    """)
    assert result == {"anchorTargets": ["c"], "memberTargets": ["b"]}


def test_renderer_unsupported_physical_orientation_does_not_commit_or_mutate():
    result = run("""
      for(const st of compare.states.values()) st.pixelSizeUm={x:.25,y:.5};
      for(const sid of compare.members) {
        compare.pairs.get(sid).transform=null;ensurePairTransform(sid);
      }
      const before=JSON.stringify(compare.pairs.get('b'));
      changeOrientation('rotate-right');replyAll();
      ({unchanged:JSON.stringify(compare.pairs.get('b'))===before,
        display:messages.filter(m=>m.nd2wsi==='display-transform').length});
    """)
    assert result == {"unchanged": True, "display": 0}


@pytest.mark.parametrize("entry", [
    "commitLandmarkEdit(editId)",
    "finishLandmarks(true)",
    "clickControl('compare-landmark-done')",
    "clickControl('compare-align')",
    "pressKey('Enter')",
    "paneMessage('b',{...landmarkMessage('b',squarePoints),nd2wsi:'landmark-done'})",
])
def test_incomplete_member_prevents_every_pair_from_committing(entry):
    result = run(f"""
      const before=JSON.stringify([...compare.pairs]);
      startLandmarks();
      putLandmarks('a',squarePoints);putLandmarks('b',squarePoints);
      putLandmarks('c',squarePoints.slice(0,3));
      const editId=compare.landmark.edit.editId;
      {entry};
      ({{unchanged:JSON.stringify([...compare.pairs])===before,
        active:compare.landmark.active,memory:compare.memory.size,
        b:compare.landmark.edit.candidates.get('b').status,
        c:compare.landmark.edit.candidates.get('c').status}});
    """)
    assert result == {
        "unchanged": True, "active": True, "memory": 0,
        "b": "valid", "c": "incomplete",
    }


def test_four_point_commit_atomically_publishes_all_pairs_with_exact_provenance():
    result = run("""
      const committed=fitAll();
      const pairs=[...compare.pairs.values()];
      ({committed,editing:compare.landmark.active,memory:compare.memory.size,
        revision:compare.committedRevision,
        counts:pairs.map(p=>p.fit.pairs),
        provenance:pairs.every(p=>p.provenance.anchorSetId===compare.anchorSet.id &&
          p.provenance.anchorRevision===compare.anchorSet.revision &&
          p.provenance.memberRevision===p.landmarkSet.revision &&
          p.provenance.anchorPointIds.join()===compare.anchorSet.points.map(v=>v.id).join()),
        fitRms:pairs.map(p=>p.fit.rms),currentRms:pairs.map(p=>p.currentRms)});
    """)
    assert result["committed"] is True
    assert result["editing"] is False
    assert result["memory"] == 2
    assert result["revision"] == 1
    assert result["counts"] == [4, 4]
    assert result["provenance"] is True
    assert all(value < 1e-9 for value in result["fitRms"] + result["currentRms"])


def test_deleting_to_one_point_invalidates_candidate_but_preserves_last_commit():
    result = run("""
      fitAll();
      const before=JSON.stringify([...compare.pairs]);
      startLandmarks();
      putLandmarks('b',squarePoints.slice(0,1));
      const candidate=compare.landmark.edit.candidates.get('b');
      const accepted=finishLandmarks(true);
      ({accepted,status:candidate.status,fit:candidate.fit,
        previousShown:JSON.stringify([...compare.pairs])===before,
        editing:compare.landmark.active});
    """)
    assert result == {
        "accepted": False, "status": "incomplete", "fit": None,
        "previousShown": True, "editing": True,
    }


def test_clear_only_changes_draft_and_cancel_preserves_committed_orientation_fit_and_memory():
    result = run("""
      changeOrientation('flip-horizontal');replyAll();
      changeOrientation('rotate-right');replyAll();
      startLandmarks();
      putLandmarks('a',squarePoints);
      putLandmarks('b',squarePoints.map(p=>({x:p.y,y:p.x})));
      putLandmarks('c',squarePoints);
      const committed=finishLandmarks(true);
      const before=JSON.stringify([...compare.pairs]);
      const memory=JSON.stringify([...compare.memory]);
      startLandmarks();messages.length=0;
      clearAlignment();
      const cleared=compare.landmark.edit.anchorSet.points.length===0 &&
        [...compare.landmark.edit.pairs.values()].every(p=>p.landmarks.length===0);
      const emptyCommit=finishLandmarks(true);
      const noCapture=messages.every(m=>m.nd2wsi!=='viewport-request');
      finishLandmarks(false);
      ({committed,cleared,emptyCommit,noCapture,
        same:JSON.stringify([...compare.pairs])===before,
        memorySame:JSON.stringify([...compare.memory])===memory});
    """)
    assert result == {
        "committed": True, "cleared": True, "emptyCommit": False,
        "noCapture": True, "same": True, "memorySame": True,
    }


def test_cancel_rejects_all_old_reply_callbacks_and_old_edit_messages():
    result = run("""
      fitAll();startLandmarks();
      const oldEdit=compare.landmark.edit.editId;
      const stalePoints=landmarkMessage('b',squarePoints.map(p=>({x:p.x+90,y:p.y})));
      requestGroup('sync');
      const request=compare.pendingRequest;
      const callbacks=getTimerCallbacks();
      const beforePairs=JSON.stringify([...compare.pairs]);
      const beforeStates=JSON.stringify([...compare.states]);
      const beforeMemory=JSON.stringify([...compare.memory]);
      finishLandmarks(false);
      startLandmarks();
      const newEdit=compare.landmark.edit.editId;
      const draftBefore=JSON.stringify([...compare.landmark.edit.pairs]);
      receiveLandmarkPoints('b',stalePoints);
      replyAll(request,{b:{centerPx:{x:99999,y:88888}}});
      for(const callback of callbacks) callback();
      ({differentEdit:newEdit!==oldEdit,
        oldCommit:commitLandmarkEdit(oldEdit),
        pairsSame:JSON.stringify([...compare.pairs])===beforePairs,
        statesSame:JSON.stringify([...compare.states])===beforeStates,
        memorySame:JSON.stringify([...compare.memory])===beforeMemory,
        draftSame:JSON.stringify([...compare.landmark.edit.pairs])===draftBefore});
    """)
    assert result == {
        "differentEdit": True, "oldCommit": False, "pairsSame": True,
        "statesSame": True, "memorySame": True, "draftSame": True,
    }


def test_clear_revision_rejects_queued_points_even_with_higher_point_sequence():
    result = run("""
      startLandmarks();
      const stale=landmarkMessage('a',squarePoints);
      clearAlignment();
      stale.pointRevision=10000;
      receiveLandmarkPoints('a',stale);
      ({count:compare.landmark.edit.anchorSet.points.length,
        candidates:compare.landmark.edit.candidates.size,
        accepted:finishLandmarks(true)});
    """)
    assert result == {"count": 0, "candidates": 0, "accepted": False}


def test_stopping_compare_discards_draft_without_updating_saved_alignment():
    result = run("""
      fitAll();
      const saved=JSON.stringify([...compare.memory]);
      startLandmarks();
      putLandmarks('b',squarePoints.map(p=>({x:p.x+100,y:p.y})));
      const callbacks=getTimerCallbacks();
      stopCompare();
      for(const callback of callbacks)callback();
      ({enabled:compare.enabled,editing:compare.landmark.active,
        unchanged:JSON.stringify([...compare.memory])===saved});
    """)
    assert result == {"enabled": False, "editing": False, "unchanged": True}


def test_member_removal_invalidates_whole_edit_and_frozen_capture_transaction():
    result = run("""
      startLandmarks();
      putLandmarks('a',squarePoints);putLandmarks('b',squarePoints);putLandmarks('c',squarePoints);
      const oldEdit=compare.landmark.edit.editId;
      const epoch=compare.groupEpoch;
      removeMember('c',false);
      const cancelledEdit=!compare.landmark.active;
      const rejectedCommit=!commitLandmarkEdit(oldEdit);
      requestGroup('capture');
      const oldRequest=compare.pendingRequest;
      removeMember('b',false);
      replyAll(oldRequest);
      ({cancelledEdit,rejectedCommit,epochAdvanced:compare.groupEpoch>epoch,
        stopped:!compare.enabled,pending:compare.pendingRequest});
    """)
    assert result == {
        "cancelledEdit": True, "rejectedCommit": True, "epochAdvanced": True,
        "stopped": True, "pending": None,
    }


def test_site_roundtrip_restores_owned_fit_without_reviving_first_visit_callbacks():
    result = run("""
      const initial={...compare.states.get('b'),spatialContext:{
        key:'source-b-P1',kind:'plate',p:1,sourceGeneration:'gen-b'}};
      receiveViewportState({...initial,nd2wsi:'viewport-ready',version:2},'b');
      const committed=fitAll();
      const first=JSON.stringify(compare.pairs.get('b'));
      requestGroup('capture');
      const stale=compare.pendingRequest;
      const oldReply={...compare.states.get('b'),...stale.contexts.get('b'),
        nd2wsi:'viewport-state',version:2,requestId:stale.requestId,seq:9999,
        centerPx:{x:9999,y:9999}};
      receiveViewportState({...initial,contextEpoch:2,seq:2,
        spatialContext:{key:'source-b-P2',kind:'plate',p:2,sourceGeneration:'gen-b'},
        nd2wsi:'viewport-ready',version:2},'b');
      const p2Unaligned=compare.pairs.get('b').fit===null;
      receiveViewportState({...initial,contextEpoch:3,seq:3,
        nd2wsi:'viewport-ready',version:2},'b');
      const restored=JSON.stringify(compare.pairs.get('b'))===first;
      const state=JSON.stringify(compare.states.get('b'));
      receiveViewportState(oldReply,'b');
      receiveViewportState({...oldReply,requestId:null,nd2wsi:'viewport-ready'},'b');
      ({committed,p2Unaligned,restored,
        stateSame:JSON.stringify(compare.states.get('b'))===state,
        contextEpoch:compare.states.get('b').contextEpoch});
    """)
    assert result == {
        "committed": True, "p2Unaligned": True, "restored": True,
        "stateSame": True, "contextEpoch": 3,
    }


def test_reload_accepts_new_instance_sequence_one_and_retires_old_high_sequence():
    result = run("""
      const old={...compare.states.get('b'),seq:1000};
      compare.states.set('b',old);
      receiveViewportState({...old,version:2,nd2wsi:'viewport-ready',
        paneInstanceId:'new-pane-b',seq:1},'b');
      const accepted=compare.states.get('b').seq===1;
      receiveViewportState({...old,version:2,nd2wsi:'viewport-ready',seq:99999},'b');
      ({accepted,instance:compare.states.get('b').paneInstanceId,
        seq:compare.states.get('b').seq});
    """)
    assert result == {"accepted": True, "instance": "new-pane-b", "seq": 1}


def test_inferred_mirror_reedit_keeps_committed_parity_not_stale_manual_orientation():
    result = run("""
      startLandmarks();changeControl('compare-mirror-policy','infer');
      putLandmarks('a',squarePoints);
      putLandmarks('b',squarePoints.map(p=>({x:600-p.x,y:p.y})));
      putLandmarks('c',squarePoints);
      const committed=finishLandmarks(true);
      const manualUnreflected=!Align.mirrored(compare.pairs.get('b').orientation);
      const previousReflected=compare.pairs.get('b').fit.reflected;
      startLandmarks();
      const candidate=compare.landmark.edit.candidates.get('b');
      ({committed,manualUnreflected,previousReflected,
        policy:compare.landmark.edit.reflectionPolicy,
        currentReflected:candidate.fit.reflected,status:candidate.status});
    """)
    assert result == {
        "committed": True, "manualUnreflected": True, "previousReflected": True,
        "policy": "keep", "currentReflected": True, "status": "valid",
    }


def test_mirror_policy_change_invalidates_old_input_and_rejects_collinear_inference():
    result = run("""
      const line=[{x:100,y:100},{x:200,y:200},{x:300,y:300},{x:400,y:400}];
      startLandmarks();
      for(const sid of groupSids())putLandmarks(sid,line);
      const old=landmarkMessage('b',squarePoints);
      const fixedValid=landmarkCommitReady();
      changeControl('compare-mirror-policy','infer');
      receiveLandmarkPoints('b',old);
      ({fixedValid,accepted:finishLandmarks(true),
        status:compare.landmark.edit.candidates.get('b').status,
        reason:compare.landmark.edit.candidates.get('b').reason,
        inputStayedLine:JSON.stringify(compare.landmark.edit.pairs.get('b').landmarks.map(p=>({x:p.x,y:p.y})))===JSON.stringify(line)});
    """)
    assert result == {
        "fixedValid": True, "accepted": False, "status": "degenerate",
        "reason": "mirror-needs-noncollinear-landmarks", "inputStayedLine": True,
    }


def test_unsupported_anisotropic_fit_blocks_atomic_commit_and_relative_is_explicit():
    result = run("""
      for(const st of compare.states.values())st.pixelSizeUm={x:.25,y:.5};
      for(const sid of compare.members){compare.pairs.get(sid).transform=null;ensurePairTransform(sid);}
      const before=JSON.stringify([...compare.pairs]);
      startLandmarks();
      const t=37*Math.PI/180;
      putLandmarks('a',squarePoints);
      putLandmarks('b',squarePoints.map(p=>({
        x:Math.cos(t)*p.x-2*Math.sin(t)*p.y+600,
        y:.5*Math.sin(t)*p.x+Math.cos(t)*p.y+100
      })));
      putLandmarks('c',squarePoints);
      const candidate=compare.landmark.edit.candidates.get('b');
      const accepted=finishLandmarks(true);
      const unchanged=JSON.stringify([...compare.pairs])===before;
      finishLandmarks(false);
      changeControl('compare-mapping','relative');replyAll();
      ({accepted,unchanged,physicalFit:candidate.fit.rms<1e-9,
        renderer:candidate.renderer.supported,
        mode:compare.pairs.get('b').mode,explicit:compare.pairs.get('b').forceRelative});
    """)
    assert result == {
        "accepted": False, "unchanged": True, "physicalFit": True,
        "renderer": False, "mode": "normalized", "explicit": True,
    }


def test_nudge_updates_current_rms_and_inverse_offset_without_refitting_on_pan():
    result = run(NUDGE_REPLY + """
      compare.members=['b'];compare.pairs.delete('c');fitAll();
      const pair=compare.pairs.get('b');
      const anchor=compare.states.get('a'),member=compare.states.get('b');
      const fittedCenter=Align.apply(pair.fitTransform,pxToSpace(anchor.centerPx,anchor,'physical'));
      nudgeAlignment(1,0,'b');
      replyNudge({x:100/member.pixelSizeUm.x,y:0}, {
        centerPx:spaceToPx({x:fittedCenter.x+100,y:fittedCenter.y},member,'physical')});
      const before={fit:pair.fit.rms,current:pair.currentRms,offset:{...pair.manualOffset},scale:pair.fit.scale};
      const originalResidual=Align.residual;let computations=0;
      Align.residual=(...args)=>{computations++;return originalResidual(...args);};
      for(let i=0;i<10;i++)forwardViewport('a',compare.states.get('a'));
      const noRecompute=computations===0;
      swapComparedSlides();
      const reversed=compare.pairs.get('a');
      ({before,noRecompute,reverse:{fit:reversed.fit.rms,current:reversed.currentRms,
        offset:reversed.manualOffset,scale:reversed.fit.scale}});
    """)
    assert result["before"]["fit"] < 1e-9
    assert result["before"]["current"] == pytest.approx(100)
    assert result["before"]["offset"] == pytest.approx({"x": 100, "y": 0})
    assert result["noRecompute"] is True
    assert result["reverse"]["fit"] < 1e-9
    assert result["reverse"]["current"] == pytest.approx(100 / result["before"]["scale"])
    assert result["reverse"]["offset"] == pytest.approx({"x": -100 / result["before"]["scale"], "y": 0})


def test_nudge_after_click_zoom_uses_delta_not_stale_anchor_center_or_span():
    result = run(NUDGE_REPLY + """
      compare.members=['b'];compare.pairs.delete('c');fitAll();
      const pair=compare.pairs.get('b'), member=compare.states.get('b');
      compare.states.get('a').centerPx={x:99999,y:88888};
      compare.states.get('a').spanPx={x:400,y:300};
      member.spanPx={x:160,y:120};
      nudgeAlignment(1,0,'b');messages.length=0;
      replyNudge({x:-.5,y:0},{centerPx:{x:500,y:400}});
      const views=messages.filter(m=>m.nd2wsi==='viewport-apply');
      ({offset:pair.manualOffset,current:pair.currentRms,fit:pair.fit.rms,
        targets:views.map(m=>m.sid),anchorSpan:views.find(m=>m.sid==='a').spanPx.x});
    """)
    assert result["offset"] == pytest.approx({"x": -.33, "y": 0})
    assert result["current"] == pytest.approx(.33)
    assert result["fit"] < 1e-9
    assert result["targets"] == ["a"]
    assert result["anchorSpan"] == pytest.approx(160)


def test_rapid_nudge_keys_serialize_and_use_fresh_committed_revisions():
    result = run(NUDGE_REPLY + """
      fitAll();
      const start=compare.committedRevision;
      nudgeAlignment(1,0,'b');nudgeAlignment(1,0,'b');nudgeAlignment(0,1,'c');
      const before=messages.filter(m=>m.nd2wsi==='viewport-nudge').length;
      const revisions=[];
      for(const delta of [{x:-.5,y:0},{x:-.5,y:0},{x:0,y:-.5}]) {
        revisions.push(compare.pendingNudge.committedRevision-start);replyNudge(delta);
      }
      ({before,revisions,b:compare.pairs.get('b').manualOffset,
        c:compare.pairs.get('c').manualOffset,pending:compare.pendingNudge,
        queued:compare.nudgeQueue.length});
    """)
    assert result["before"] == 1
    assert result["revisions"] == [0, 1, 2]
    assert result["b"] == pytest.approx({"x": -.66, "y": 0})
    assert result["c"] == pytest.approx({"x": 0, "y": -.25})
    assert result["pending"] is None
    assert result["queued"] == 0


def test_nudge_reply_after_fit_revision_change_never_updates_state_or_alignment():
    result = run(NUDGE_REPLY + """
      fitAll();nudgeAlignment(1,0,'b');nudgeAlignment(1,0,'b');
      const pair=JSON.stringify(compare.pairs.get('b'));
      const state=JSON.stringify(compare.states.get('b'));
      compare.committedRevision++; // another committed operation supersedes this key
      const accepted=replyNudge({x:100,y:100},{centerPx:{x:98765,y:43210}});
      ({accepted,pairSame:JSON.stringify(compare.pairs.get('b'))===pair,
        stateSame:JSON.stringify(compare.states.get('b'))===state,
        pending:compare.pendingNudge,queued:compare.nudgeQueue.length});
    """)
    assert result == {
        "accepted": False, "pairSame": True, "stateSame": True, "pending": None, "queued": 0,
    }


def test_pending_nudge_blocks_orientation_and_align_but_old_timeout_cannot_clear_new_key():
    result = run(NUDGE_REPLY + """
      nudgeAlignment(1,0,'b');
      const timer=getTimerCallbacks().at(-1), first=compare.pendingNudge;
      changeOrientation('transpose');startLandmarks();
      const blocked=!compare.pendingRequest&&!compare.landmark.active;
      replyNudge({x:-.5,y:0});nudgeAlignment(1,0,'c');
      const second=compare.pendingNudge;timer();
      ({blocked,different:first!==second,preserved:compare.pendingNudge===second});
    """)
    assert result == {"blocked": True, "different": True, "preserved": True}


@pytest.mark.parametrize("angle,reflected", [(0, False), (90, True), (37, False), (37, True)])
def test_anchor_drag_delta_inverse_composition_keeps_member_landmarks_fixed(angle, reflected):
    result = run(f"""
      compare.members=['b'];compare.pairs.delete('c');
      const angle={angle}*Math.PI/180, sign={-1 if reflected else 1};
      const pair=compare.pairs.get('b');
      pair.transform={{a:2.7*Math.cos(angle),b:-2.7*Math.sin(angle)*sign,
        c:2.7*Math.sin(angle),d:2.7*Math.cos(angle)*sign,tx:50,ty:-70}};
      const old={{...pair.transform}},point={{x:135,y:98}},delta={{x:7,y:-9}};
      const d=pxToSpace(delta,compare.states.get('a'),'physical');
      applyNudgeDelta('a',delta);
      ({{expected:Align.apply(old,point),actual:Align.apply(pair.transform,
        {{x:point.x+d.x,y:point.y+d.y}})}});
    """)
    assert result["actual"] == pytest.approx(result["expected"])


@pytest.mark.parametrize("cancelled,stale", [(True, False), (False, True)])
def test_cancelled_or_stale_drag_packet_never_writes_global_snapshot(cancelled, stale):
    result = run(f"""
      fitAll();const member=compare.states.get('b');
      const beforeState=JSON.stringify(member),beforePairs=JSON.stringify([...compare.pairs]);
      const accepted=receiveViewportState({{...member,...spatialEnvelope('b'),version:2,
        seq:member.seq+1,nudge:true,nudgeSource:'drag',dragId:'actual-drag',
        nudgeCancelled:{str(cancelled).lower()},committedRevision:compare.committedRevision-{int(stale)},
        nudgeDeltaPx:{{x:100,y:200}},centerPx:{{x:99999,y:77777}}}},'b');
      ({{accepted,stateSame:JSON.stringify(compare.states.get('b'))===beforeState,
        pairsSame:JSON.stringify([...compare.pairs])===beforePairs}});
    """)
    assert result == {"accepted": False, "stateSame": True, "pairsSame": True}


def test_restoring_different_anchor_set_is_marked_mismatched_not_silently_valid():
    result = run("""
      fitAll();
      const oldB=cloneValue(compare.memory.get(pairKey('a','b')));
      startLandmarks();clearAlignment();
      for(const sid of groupSids())putLandmarks(sid,squarePoints.map(p=>({x:p.x+10,y:p.y+20})));
      finishLandmarks(true);
      compare.memory.set(pairKey('a','b'),oldB);
      const restored=newPair();
      restoreAlignment('a','b',restored);
      ({mismatch:restored.provenanceMismatch,currentRms:restored.currentRms,
        note:orientationNote(restored),
        differentSet:restored.provenance.anchorSetId!==compare.anchorSet.id});
    """)
    assert result == {
        "mismatch": True, "currentRms": None,
        "note": "Stored transform · Landmark set mismatch", "differentSet": True,
    }


def test_controls_render_does_not_write_memory_or_mutate_committed_alignment():
    result = run("""
      fitAll();
      const pairs=JSON.stringify([...compare.pairs]);
      const memory=JSON.stringify([...compare.memory]);
      const revision=compare.committedRevision;
      for(let i=0;i<20;i++)renderRealCompareControls();
      ({pairsSame:JSON.stringify([...compare.pairs])===pairs,
        memorySame:JSON.stringify([...compare.memory])===memory,
        revisionSame:compare.committedRevision===revision});
    """)
    assert result == {"pairsSame": True, "memorySame": True, "revisionSame": True}


def test_hiding_and_reopening_toolbox_preserves_draft_and_pending_transaction():
    result = run("""
      startLandmarks();putLandmarks('a',squarePoints);
      requestGroup('sync');
      const pending=compare.pendingRequest,edit=compare.landmark.edit;
      const epoch=compare.groupEpoch;
      clickControl('compare-close');
      const hidden=!compare.toolsVisible;
      setCompareToolsVisible(true);
      ({hidden,visible:compare.toolsVisible,linked:compare.linked,
        sameEdit:compare.landmark.edit===edit,sameRequest:compare.pendingRequest===pending,
        sameEpoch:compare.groupEpoch===epoch});
    """)
    assert result == {
        "hidden": True, "visible": True, "linked": True,
        "sameEdit": True, "sameRequest": True, "sameEpoch": True,
    }
