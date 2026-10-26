"""Exercise development stage-follow code in a private native Agent window.

Uses the installed app's Python/UI dependencies with this checkout's code.
No controller connection, raw protocol decoding, or shared-cache rebuild.
"""
import os
import sys
from pathlib import Path

checkout = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(checkout))
os.environ['ND2WSI_DISABLE_UPDATES'] = '1'
os.environ['ND2WSI_APP_NAME'] = 'nd2wsi Stage follow development check'

import hashlib
import json
import math
import threading
import time
import traceback
import urllib.request
import nd2wsi
import webview
from nd2wsi import app
from nd2wsi.translucent_smoke import (
    bridge_result, camera, drag_image, native_state, pane_eval, save_cocoa_snapshot, wait_for,
)
from nd2wsi.window_sessions import create_window_session

assert Path(nd2wsi.__file__).resolve().parent == checkout/'nd2wsi', 'Development source not selected'
qa = Path(os.environ['ASTROMAP_QA_ROOT'])
data = Path('/Volumes/Extreme SSD/workspace/ExM-SLACS/261026_SLACS_ITO_calibration')
source = data/'Merged_SLACS_ITO.nd2'
cache = data/'nd2wsi/caches/Merged_SLACS_ITO.nd2--t0-p0-zmid.nd2svs'
annotations = data/'nd2wsi/annotations/annotations_Merged_SLACS_ITO.nd2--t0-p0-z0.json'
legacy = Path('/Users/myunghyunjeong/Desktop/2610256 ExM SLACS')
protected = [source, cache, annotations, data/'context.md', legacy/'SLACS.py', legacy/'LPC_ACS.py']
def fingerprints():
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
before = fingerprints()
session = create_window_session('agent', qa/'sessions')
app._dlog = lambda message: None
api, parent = app.create_app_window(None, window_session=session)
report = {'ok': False, 'session': session.as_dict(), 'source': str(source),
          'module': nd2wsi.__file__, 'platform': sys.platform, 'cases': [],
          'scope': 'synthetic process session and normalized commands through real native UI; no real discovery or ACS capture'}
heartbeat_stop = threading.Event()
heartbeat_thread = None
descriptor = None

class SyntheticDiscovery:
    def __init__(self):
        self.state = {'available': True, 'session_id': 'native-qa', 'status': 'running'}
    def snapshot(self):
        return dict(self.state)
    def stop(self):
        return True

def send(route, **event):
    body = {'version': 1, 'session_id': descriptor['session_id'], **event}
    req = urllib.request.Request(descriptor['url']+route, data=json.dumps(body).encode(),
        headers={'Content-Type':'application/json','Authorization':'Bearer '+descriptor['token']}, method='POST')
    with urllib.request.urlopen(req,timeout=2) as response:
        return json.load(response)

def heartbeat():
    while not heartbeat_stop.wait(.4):
        try: send('/heartbeat')
        except Exception: return

def close_enough(actual, expected):
    return max(abs(a-b) for a,b in zip(actual,expected)) < 1e-6

def motion(child, seq, expected, **event):
    response = send('/motion',seq=seq,**event)
    wait_for(lambda: child.evaluate_js('nd2Translucent.state.follow.seq')==seq,
             description='commanded movement rendered')
    actual = camera(child)['center']
    assert close_enough(actual,expected), (actual,expected)
    report['cases'].append({'seq':seq,'type':event['type'],'actual_center':actual,
                            'expected_center':expected,'max_error_px':max(abs(a-b) for a,b in zip(actual,expected))})
    return response

def run():
    global descriptor, heartbeat_thread
    try:
        wait_for(lambda: parent.evaluate_js('!!window.pywebview?.api'),description='private Agent bridge')
        context = bridge_result(parent,'pywebview.api.window_context()')
        assert context['role']=='agent' and context['id']==session.id
        api._httpd,url = app.start_server(cache,window_session=session)
        parent.load_url(url)
        wait_for(lambda: pane_eval(parent,'!!(state.info && state.annReady && state.viewer.isOpen())'),
                 timeout=45,description='ITO source image')
        assert parent.evaluate_js("document.getElementById('slacs-offer').hidden"), 'Offer visible without detection'
        if api._slacs_discovery is not None:
            assert api._slacs_discovery.stop() is True
        synthetic = SyntheticDiscovery()
        api._slacs_discovery = synthetic
        wait_for(lambda: parent.evaluate_js("!document.getElementById('slacs-offer').hidden"),
                 description='synthetic process offer')
        report['synthetic_main_offer_verified'] = True
        sid,store=next(iter(api._httpd.registry.slides.items()))
        with urllib.request.urlopen(url.rstrip('/')+'/s/'+sid+'/api/info') as response:
            info=json.load(response)
        mpp=info['pixelSizeUm']; origin=[1024.,1024.]
        # Synthetic test outlines are private and never saved to research annotations.
        rois=[{'x':950,'y':950,'w':100,'h':100,'index':1,'label':'QA outline'}]
        result=api.open_translucent_viewer(sid,{'version':1,'center':origin,'zoom':1,
            'rotation':0,'flip':False,'query':'c=0%2C1&g='+info['generation'],'rois':rois,'roiIndex':0})
        assert result['ok'],result
        overlay=api._translucent_windows[sid]; child=overlay._window
        wait_for(lambda: child.evaluate_js('!!window.nd2Translucent?.state.ready'),description='translucent ready')
        assert child.evaluate_js("document.getElementById('stage-follow-control').hidden"), 'Follow visible without detection'
        overlay.slacs_discovery_state()
        assert overlay._discovery.stop() is True
        overlay._on_slacs_change(synthetic.snapshot())
        wait_for(lambda: child.evaluate_js("nd2Translucent.state.slacs.session_id === 'native-qa' && !document.getElementById('stage-follow-control').hidden"),
                 description='synthetic process overlay controls')
        report['synthetic_overlay_visibility_verified'] = True
        if os.environ.get('ND2WSI_SLACS_VISIBILITY_ONLY') == '1':
            # Verify native UI/lifecycle without moving the shared mouse or keyboard.
            assert bridge_result(child, 'nd2Translucent.startStageFollow()')
            receiver = overlay._stage_follow
            synthetic.state = {'available': False, 'session_id': None, 'status': 'not_running'}
            overlay._on_slacs_change(synthetic.snapshot())
            wait_for(lambda: child.evaluate_js("document.getElementById('stage-follow-control').hidden && !document.getElementById('stage-follow-disconnected').hidden"),
                     description='process loss hides controls and explains stopped follow')
            wait_for(lambda: parent.evaluate_js("document.getElementById('slacs-offer').hidden"),
                     description='process loss hides main offer')
            assert not receiver.snapshot()['enabled']
            synthetic.state = {'available': True, 'session_id': 'native-qa-new', 'status': 'running'}
            overlay._on_slacs_change(synthetic.snapshot())
            wait_for(lambda: child.evaluate_js("nd2Translucent.state.slacs.session_id === 'native-qa-new' && !document.getElementById('stage-follow-control').hidden"),
                     description='new synthetic process session')
            assert not child.evaluate_js('nd2Translucent.state.follow.enabled')
            report['synthetic_loss_and_no_auto_resume_verified'] = True
            report['ok'] = True
            return
        wait_for(lambda: child.evaluate_js('nd2Translucent.viewer.world.getItemAt(0).getFullyLoaded()'),
                 timeout=45,description='real ITO tiles')
        native_before=native_state(child)
        assert camera(child)['flip'] is True, 'Default mirror image is not active'
        report['default_mirror_verified']=True
        roi_before=child.evaluate_js('JSON.stringify(nd2Translucent.state.rois)')
        roi_screen_before=child.evaluate_js("document.querySelector('#roi-overlay polygon').getAttribute('points')")
        marker_before=child.evaluate_js('Nd2ViewerGeometry.imageToViewerElementPoint(nd2Translucent.viewer.viewport,{x:1000,y:1000},OpenSeadragon)')
        assert bridge_result(child,'nd2Translucent.startStageFollow()')
        descriptor=overlay.start_stage_follow()
        heartbeat_thread=threading.Thread(target=heartbeat,daemon=True); heartbeat_thread.start()
        bridge_result(child,'nd2Translucent.toggleLock().then(()=>true)')
        assert child.evaluate_js('nd2Translucent.state.locked')
        motion(child,1,origin,type='reference',position_um=[10000,5000])
        step=[origin[0]+1/mpp[1],origin[1]]
        motion(child,2,step,type='delta',delta_um=[1,0])
        marker_after=child.evaluate_js('Nd2ViewerGeometry.imageToViewerElementPoint(nd2Translucent.viewer.viewport,{x:1000,y:1000},OpenSeadragon)')
        assert math.isclose(marker_after['x']-marker_before['x'],1/mpp[1],abs_tol=1e-6)
        report['mirrored_screen_delta_css_px']=[marker_after['x']-marker_before['x'],marker_after['y']-marker_before['y']]
        roi_screen_after=child.evaluate_js("document.querySelector('#roi-overlay polygon').getAttribute('points')")
        assert roi_screen_before!=roi_screen_after
        assert child.evaluate_js('JSON.stringify(nd2Translucent.state.rois)')==roi_before
        target=[origin[0]+101/mpp[1],origin[1]+10/mpp[0]]
        motion(child,3,target,type='target',position_um=[10101,5010])
        bridge_result(child,'nd2Translucent.toggleLock().then(()=>true)')
        assert not child.evaluate_js('nd2Translucent.state.locked')
        drag_image(child)
        wait_for(lambda: not close_enough(camera(child)['center'],target),description='unlocked manual pan')
        time.sleep(.25)
        manual=camera(child)['center']
        manual_next=[manual[0]+10/mpp[1],manual[1]]
        motion(child,4,manual_next,type='delta',delta_um=[10,0])
        bridge_result(child,'nd2Translucent.toggleLock().then(()=>true)')
        drag_image(child)
        time.sleep(.2)
        assert close_enough(camera(child)['center'],manual_next),'Locked manual gesture moved image'
        bridge_result(child,'nd2Translucent.toggleLock().then(()=>true)')
        assert child.evaluate_js('nd2Translucent.setMirrorScan(false)')
        child.evaluate_js('nd2Translucent.viewer.viewport.setRotation(90,true); true')
        bridge_result(child,'nd2Translucent.toggleLock().then(()=>true)')
        oriented=[manual_next[0]-10/mpp[1],manual_next[1]]
        motion(child,5,oriented,type='delta',delta_um=[-10,0])
        assert camera(child)['rotation']==90 and camera(child)['flip'] is False
        assert math.isclose(camera(child)['zoom'],1,abs_tol=1e-9)
        send('/motion',seq=5,type='delta',delta_um=[-10,0])
        time.sleep(.15)
        assert close_enough(camera(child)['center'],oriented),'Duplicate command applied twice'
        report['pixel_size_um_yx']=mpp
        report['pixels_per_roughness1']=1/mpp[1]
        report['manual_anchor_preserved']=True
        report['locked_manual_pan_blocked']=True
        report['roi_coordinates_unchanged']=child.evaluate_js('JSON.stringify(nd2Translucent.state.rois)')==roi_before
        report['roi_screen_moved']=roi_screen_before!=roi_screen_after
        report['native_frame_preserved']=native_state(child)['frame']==native_before['frame']
        report['opacity_preserved']=child.evaluate_js('nd2Translucent.state.opacity')==.55
        report['render_query_preserved']=overlay.translucent_context()['query']=='?c=0%2C1&g='+info['generation']
        bridge_result(child,'nd2Translucent.toggleLock().then(()=>true)')
        assert child.evaluate_js('nd2Translucent.setMirrorScan(true)')
        child.evaluate_js('nd2Translucent.viewer.viewport.setRotation(0,true); true')
        bridge_result(child,'nd2Translucent.toggleLock().then(()=>true)')
        child.evaluate_js("document.getElementById('stage-follow-options').click(); true")
        report['snapshot']=str(save_cocoa_snapshot(child,session.exports_root/'stage-follow-native.png'))
        try: send('/motion',seq=7,type='delta',delta_um=[100,0])
        except urllib.error.HTTPError as exc: assert exc.code==409
        wait_for(lambda: child.evaluate_js('nd2Translucent.state.follow.status')=='paused',description='gap pause')
        assert close_enough(camera(child)['center'],oriented),'Gap moved image'
        report['gap_paused_without_pan']=True
        old_receiver = overlay._stage_follow
        synthetic.state = {'available': False, 'session_id': None, 'status': 'not_running'}
        overlay._on_slacs_change(synthetic.snapshot())
        wait_for(lambda: child.evaluate_js("document.getElementById('stage-follow-control').hidden && !document.getElementById('stage-follow-disconnected').hidden"),
                 description='process loss hides controls and explains stopped follow')
        wait_for(lambda: parent.evaluate_js("document.getElementById('slacs-offer').hidden"),
                 description='process loss hides main offer')
        assert not old_receiver.snapshot()['enabled']
        synthetic.state = {'available': True, 'session_id': 'native-qa-new', 'status': 'running'}
        overlay._on_slacs_change(synthetic.snapshot())
        wait_for(lambda: child.evaluate_js("nd2Translucent.state.slacs.session_id === 'native-qa-new' && !document.getElementById('stage-follow-control').hidden"),
                 description='new synthetic process session')
        assert not child.evaluate_js('nd2Translucent.state.follow.enabled')
        report['synthetic_loss_and_no_auto_resume_verified'] = True
        assert all(report[k] for k in ('roi_coordinates_unchanged','roi_screen_moved','native_frame_preserved',
                                      'opacity_preserved','render_query_preserved'))
        report['ok']=True
    except Exception:
        report['error']=traceback.format_exc()
    finally:
        heartbeat_stop.set()
        if heartbeat_thread: heartbeat_thread.join(timeout=3)
        api._close_translucent_windows()
        report['sources_unchanged']=fingerprints()==before
        report['source_hashes']=before
        if not report['sources_unchanged']: report['ok']=False
        (qa/os.environ['ASTROMAP_QA_REPORT']).write_text(json.dumps(report,ensure_ascii=False,indent=2))
        (session.exports_root/'stage-follow-native.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        parent.destroy()

try:
    webview.start(run,private_mode=True)
finally:
    if api._httpd is not None: api.stop_server_for_update()
    session.mark_closed()
