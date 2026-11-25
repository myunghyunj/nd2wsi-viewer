"""Exercise only a newly-created private Agent window, without OS input."""
import importlib.util
import io
import json
import os
import shutil
import sys
import threading
import time
import traceback
import urllib.request
import zipfile
from pathlib import Path

os.environ['ND2WSI_DISABLE_UPDATES'] = '1'
os.environ['ND2WSI_APP_NAME'] = 'nd2wsi-viewer Astromap Beta QA'

if importlib.util.find_spec('nd2wsi') is None:
    _ROOT = Path(__file__).resolve().parents[1]
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))

import webview  # noqa: E402

import nd2wsi  # noqa: E402
from nd2wsi import app  # noqa: E402
from nd2wsi.convert import ensure_cache, existing_cache_store  # noqa: E402
from nd2wsi.window_sessions import create_window_session  # noqa: E402

qa = Path(os.environ['ASTROMAP_QA_ROOT']).resolve()
report_path = qa / os.environ.get('ASTROMAP_QA_REPORT', 'native-check.json')
session = create_window_session('agent', qa / 'sessions')
api, window = app.create_app_window(None, window_session=session)
report = {'ok': False, 'module': nd2wsi.__file__, 'session': session.as_dict()}


def check(value, message):
    if not value:
        raise AssertionError(message)


def wait_js(expression, timeout=60):
    start = time.monotonic()
    last = None
    while time.monotonic() - start < timeout:
        try:
            last = window.evaluate_js(expression)
            if last:
                return last
        except Exception as exc:
            last = str(exc)
        time.sleep(.1)
    raise TimeoutError(f'{expression}: {last}')


def run():
    try:
        context = api.window_context()
        check(context['role'] == 'agent' and context['id'] == session.id, 'wrong window role')
        wait_js('!!window.pywebview?.api')
        # Ask this window's bridge to confirm identity before opening a slide.
        window.evaluate_js('pywebview.api.window_context().then(x => window.__qaContext=x); true')
        bridged = wait_js('window.__qaContext || null')
        check(bridged['id'] == session.id and bridged['role'] == 'agent', 'bridge identity mismatch')
        window.show()
        source = Path(os.environ.get('ASTROMAP_QA_INPUT', str(qa.parent / 'docs/example_tissue.nd2'))).resolve()
        if source.suffix.lower() != '.nd2svs' and existing_cache_store(source, read_only=True) is None:
            isolated_dir = qa / 'isolated-input'
            isolated_dir.mkdir(parents=True, exist_ok=True)
            isolated_copy = isolated_dir / source.name
            if not isolated_copy.exists():
                shutil.copy2(source, isolated_copy)
            source = ensure_cache(isolated_copy)
        url = api.open_path(str(source))
        check(url, f'open failed: {api._status}')
        window.load_url(url)
        wait_js("(() => {const f=document.querySelector('#frames iframe.active'); const s=f?.contentWindow?.nd2ViewerState; return !!(s && s.info && s.annReady && !!s.windows.astromap);})()")
        report['url'] = url
        check(window.evaluate_js("!document.getElementById('astromap-toggle').hidden"), 'star hidden initially')
        window.evaluate_js("document.getElementById('astromap-toggle').click(); true")
        wait_js("!document.querySelector('#frames iframe.active').contentDocument.getElementById('win-astromap').classList.contains('hidden')")
        window.evaluate_js("document.getElementById('astromap-toggle').click(); true")
        wait_js("document.querySelector('#frames iframe.active').contentDocument.getElementById('win-astromap').classList.contains('hidden')")
        report['star_visible_before_shortcut'] = True
        # Cmd alone must not open the panel or change star visibility.
        window.evaluate_js("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Meta',metaKey:true,bubbles:true})); true")
        time.sleep(1.1)
        window.evaluate_js("document.dispatchEvent(new KeyboardEvent('keyup',{key:'Meta',bubbles:true})); true")
        check(window.evaluate_js("!document.getElementById('astromap-toggle').hidden"), 'star became hidden')
        # Cmd+Shift+A from the active slide must open its panel.
        window.evaluate_js("document.querySelector('#frames iframe.active').contentDocument.dispatchEvent(new KeyboardEvent('keydown',{key:'A',code:'KeyA',metaKey:true,shiftKey:true,bubbles:true,cancelable:true})); true")
        wait_js("!document.getElementById('astromap-toggle').hidden", 4)
        report['astromap_shortcut'] = True
        check(window.evaluate_js("document.getElementById('astromap-toggle').nextElementSibling.id === 'shading-control'"), 'toolbar order')
        check(window.evaluate_js("document.getElementById('astromap-toggle').textContent.trim()===''"), 'toolbar text visible')
        wait_js("!document.querySelector('#frames iframe.active').contentDocument.getElementById('win-astromap').classList.contains('hidden')")
        # The star continues to toggle the panel after keyboard activation.
        window.evaluate_js("document.getElementById('astromap-toggle').click(); true")
        wait_js("document.querySelector('#frames iframe.active').contentDocument.getElementById('win-astromap').classList.contains('hidden')")
        window.evaluate_js("document.getElementById('astromap-toggle').click(); true")
        wait_js("!document.querySelector('#frames iframe.active').contentDocument.getElementById('win-astromap').classList.contains('hidden')")
        # First ROI through Select Region -> Add Current; second via the committed drag callback.
        setup = """(() => {
          const f=document.querySelector('#frames iframe.active'), v=f.contentWindow;
          const state=v.nd2ViewerState, doc=v.document;
          state.roi={x:200,y:300,w:500,h:400}; state.roiSite=null;
          v.refreshAstromapPanel(); doc.getElementById('astromap-add-current').click();
          const stage=doc.getElementById('stage'), bounds=stage.getBoundingClientRect();
          const a0=v.imageToViewerElementPoint({x:1000,y:1100});
          const b0=v.imageToViewerElementPoint({x:1300,y:1300});
          const pointer=(type,p,button=0,shift=true)=>stage.dispatchEvent(new v.PointerEvent(type,{
            pointerId:9, pointerType:'mouse', isPrimary:true, button,
            buttons:type==='pointerup'?0:1, shiftKey:shift, bubbles:true,cancelable:true,
            clientX:bounds.left+p.x,clientY:bounds.top+p.y}));
          pointer('pointerdown',a0,2); pointer('pointerup',b0,2);
          if(state.annotations.filter(x=>x.astromap===true).length!==1) throw new Error('right-click created ROI');
          pointer('pointerdown',a0); pointer('pointermove',b0); pointer('pointerup',b0);
          if(state.tool!==null || !state.viewer.isMouseNavEnabled()) throw new Error('navigation not restored');
          const a=state.annotations.filter(x=>x.astromap===true);
          if(a.length!==2) throw new Error('ROI count '+a.length);
          a[0].text='검증 ROI'; a[1].text='second';
          v.annotationsChanged(); v.refreshAstromapPanel();
          return {rois:v.Nd2Astromap.selectedRois(state.annotations),
            generation:state.info.generation,channels:state.channels,window:v.lutParam(),frame:null,
            info:state.info};
        })()"""
        data = window.evaluate_js(setup)
        report['rois'] = data['rois']
        # Capture the exact browser export request and let the UI prepare its native download.
        script = """(() => {
          const v=document.querySelector('#frames iframe.active').contentWindow;
          v.__qaFetch=v.fetch;
          v.fetch=async function(input,init){
            const result=await v.__qaFetch.call(this,input,init);
            if(String(input).includes('api/astromap') && init?.method==='POST') {
              v.__qaExportStatus=result.status;
              v.__qaExport=await result.clone().json();
            }
            return result;
          };
          v.__qaAnchorClick=v.HTMLAnchorElement.prototype.click;
          v.HTMLAnchorElement.prototype.click=function(){v.__qaDownload=this.href;};
          v.document.getElementById('astromap-export').click();
          return true;
        })()"""
        window.evaluate_js(script)
        result = wait_js("document.querySelector('#frames iframe.active').contentWindow.__qaExport || null")
        status = window.evaluate_js("document.querySelector('#frames iframe.active').contentWindow.__qaExportStatus")
        check(status == 200, f'export failed {status}: {result}')
        download = wait_js("document.querySelector('#frames iframe.active').contentWindow.__qaDownload || null")
        with urllib.request.urlopen(download, timeout=60) as response:
            body = response.read()
        path = session.exports_root / 'native-astromap-check.zip'
        path.write_bytes(body)
        from PIL import Image
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            report['members'] = z.namelist()
            txt = next(n for n in z.namelist() if n.endswith('.txt'))
            jpg = next(n for n in z.namelist() if n.endswith('.jpg'))
            report['txt'] = z.read(txt).decode('utf-8')
            check(report['txt'].startswith('INDEX\tX1\tY1\tX2\tY2\tNOTE\n'), 'TXT header mismatch')
            image = Image.open(io.BytesIO(z.read(jpg)))
            report['image_size'] = list(image.size)
            check(max(image.size) <= 8000, 'JPEG too large')
            check('検' not in report['txt'] and '검증 ROI' in report['txt'], 'UTF-8 note lost')
        report['export'] = result
        # State must remain original-sized after saving.
        after = window.evaluate_js("(() => { const v=document.querySelector('#frames iframe.active').contentWindow; return v.Nd2Astromap.selectedRois(v.nd2ViewerState.annotations); })()")
        check(after == data['rois'], 'export mutated original coordinates')
        # Saving twice must keep dimensions/coordinates and rendered image identical.
        request = urllib.request.Request(download.split('api/astromap/download')[0]+'api/astromap',
            json.dumps({k:data[k] for k in ('generation','rois','channels','window','frame')}).encode(),
            {'Content-Type':'application/json'}, method='POST')
        with urllib.request.urlopen(request,timeout=60) as response:
            repeated=json.load(response)
        repeat_url=download.split('api/astromap/download')[0]+repeated['download_url']
        with urllib.request.urlopen(repeat_url,timeout=60) as response:
            repeat_body=response.read()
        with zipfile.ZipFile(io.BytesIO(body)) as first, zipfile.ZipFile(io.BytesIO(repeat_body)) as second:
            check(all(first.read(n)==second.read(n) for n in first.namelist()), 'repeated export differs')
        report['repeated_export_identical']=True
        report['shift_primary_drag']=True
        report['updates'] = api.update_status()
        check(report['updates']['available'] is False, 'beta updater enabled')
        # Snapshot only this private Agent WKWebView, without moving OS focus.
        from AppKit import NSBitmapImageFileTypePNG, NSBitmapImageRep
        from PyObjCTools import AppHelper
        from webview.platforms.cocoa import BrowserView
        captured = threading.Event()
        snapshot = session.exports_root / 'astromap-native.png'
        def complete_snapshot(native_image, error):
            try:
                if error is not None:
                    raise RuntimeError(str(error))
                bitmap = NSBitmapImageRep.imageRepWithData_(native_image.TIFFRepresentation())
                bitmap.representationUsingType_properties_(NSBitmapImageFileTypePNG, {}).writeToFile_atomically_(str(snapshot), True)
                report['screenshot'] = str(snapshot)
            except Exception as exc:
                report['screenshot_error'] = str(exc)
            finally:
                captured.set()
        AppHelper.callAfter(lambda: BrowserView.instances[window.uid].webview.takeSnapshotWithConfiguration_completionHandler_(None, complete_snapshot))
        check(captured.wait(15), 'native snapshot timeout')
        report['ok'] = True
    except Exception:
        report['error'] = traceback.format_exc()
    finally:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
        print(json.dumps(report, ensure_ascii=False), flush=True)
        window.destroy()


try:
    webview.start(run, private_mode=True)
finally:
    if api._httpd is not None:
        api._stop_server_for_update()
    session.mark_closed()
