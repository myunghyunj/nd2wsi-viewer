"""Validate the final local beta in one private, identity-verified Agent window."""
import ast
from pathlib import Path

base = Path(__file__).with_name('check_native_astromap.py')
source = base.read_text()
orientation_tree = ast.parse(base.with_name('check_native_orientation.py').read_text())
orientation = next(ast.literal_eval(node.value) for node in orientation_tree.body
                   if isinstance(node, ast.Assign) and any(
                       isinstance(target, ast.Name) and target.id == 'extra' for target in node.targets))
extra = '''        # Reopen the exported JPEG through the actual native entry point.
        with zipfile.ZipFile(io.BytesIO(body)) as exported:
            name = next(n for n in exported.namelist() if n.endswith('.jpg'))
            jpg_path = session.exports_root / 'small-image.jpg'
            jpg_path.write_bytes(exported.read(name))
        import hashlib
        jpg_digest = hashlib.sha256(jpg_path.read_bytes()).hexdigest()
        raster_url = api.open_path(str(jpg_path))
        check(raster_url, f'JPG open failed: {api.status()}')
        window.load_url(raster_url)
        wait_js("typeof activate==='function' && slides.length===2")
        window.evaluate_js('activate(slides[slides.length-1].sid); true')
        wait_pane('state.info?.raster && state.annReady && state.viewer.isOpen()')
        check(pane('pixelSize()') is None, 'JPG fabricated physical calibration')
        pane('state.viewer.viewport.zoomTo(state.viewer.viewport.getMaxZoom(),null,true); state.viewer.viewport.applyConstraints(true); true')
        image_zoom = pane('state.viewer.viewport.viewportToImageZoom(state.viewer.viewport.getZoom(true))')
        check(image_zoom > 1, f'JPG digital zoom unavailable: {image_zoom}')
        pane('state.viewer.viewport.goHome(true); true')
        window.evaluate_js("document.getElementById('astromap-toggle').click(); true")
        wait_pane('!state.windows.astromap.isHidden()')
        pane("state.roi={x:111,y:123,w:80,h:90}; state.roiSite=null; refreshAstromapPanel(); document.getElementById('astromap-add-current').click(); true")
        check(pane('astromapItems().length') == 1, 'JPG annotation did not create ROI')
        # Exercise the ZIP button on the JPEG pane and retain the real server response.
        pane("window.__qaFetch=window.fetch; window.fetch=async function(input,init){const result=await window.__qaFetch.call(this,input,init); if(String(input).includes('api/astromap') && init?.method==='POST'){window.__qaExportStatus=result.status;window.__qaExport=await result.clone().json();}return result;}; HTMLAnchorElement.prototype.click=function(){window.__qaDownload=this.href;}; document.getElementById('astromap-export').click(); true")
        jpeg_export = wait_pane('window.__qaExport || null')
        check(pane('window.__qaExportStatus') == 200, f'JPG export failed: {jpeg_export}')
        jpeg_download = wait_pane('window.__qaDownload || null')
        with urllib.request.urlopen(jpeg_download, timeout=60) as response:
            jpeg_zip = response.read()
        jpeg_zip_path = session.exports_root / 'jpeg-annotation.zip'
        jpeg_zip_path.write_bytes(jpeg_zip)
        with zipfile.ZipFile(io.BytesIO(jpeg_zip)) as exported:
            txt_name = next(n for n in exported.namelist() if n.endswith('.txt'))
            jpg_name = next(n for n in exported.namelist() if n.endswith('.jpg'))
            jpeg_txt = exported.read(txt_name).decode('utf-8')
            row = jpeg_txt.splitlines()[1].split('\\t')
            check(row[1:5] == ['111','123','190','212'], f'JPG ROI coordinates changed: {row}')
            with Image.open(io.BytesIO(exported.read(jpg_name))) as raster_image:
                check(list(raster_image.size) == report['image_size'], 'JPG export dimensions changed')
        check(hashlib.sha256(jpg_path.read_bytes()).hexdigest() == jpg_digest, 'JPG source was modified')
        report['jpeg'] = {'digital_zoom': image_zoom, 'export': str(jpeg_zip_path), 'txt': jpeg_txt, 'source_unchanged': True}
        # Inspect confirmation only; never rebuild a slide just to check a dialog.
        minimal = window.evaluate_js("""(() => {
          document.documentElement.classList.add('light');
          document.querySelector('#frames iframe.active').contentDocument.documentElement.classList.add('light');
          window.__qaSavedShading = shadingStates.get(active);
          shadingStates.set(active,{...window.__qaSavedShading,state:'idle',applied:false,
            pending:null,refreshing:false,view_stale:false,blocked:false,supported:true,
            methods:{bffree:{supported:true},h:{supported:true}},estimated_seconds:360});
          refreshShadingButton(); activateShadingCorrection();
          const dialog=document.getElementById('shading-confirm');
          const visibleText=[...dialog.children].filter(node=>node.id!=='shading-confirm-title').map(node=>node.innerText).join(' ');
          const title=document.getElementById('shading-confirm-title').getBoundingClientRect();
          const material=getComputedStyle(dialog);
          return {open:dialog.open,text:visibleText,titleSize:[title.width,title.height],radios:dialog.querySelectorAll('input[type=radio]').length,
            method:shadingConfirmation?.method,estimate:document.getElementById('shading-confirm-estimate').textContent,
            background:material.backgroundColor,blur:material.backdropFilter || material.webkitBackdropFilter};
        })()""")
        check(minimal['open'] and minimal['radios'] == 0 and minimal['method'] == 'bffree', f'Incorrect minimal confirmation: {minimal}')
        check(minimal['titleSize'] == [1,1], 'Confirmation title is visibly rendered')
        check(minimal['background'] == 'rgba(246, 246, 248, 0.72)' and 'blur(28px)' in minimal['blur'], f'Light glass material mismatch: {minimal}')
        check(' '.join(minimal['text'].split()) == "Applying correction rebuilds and saves this slide's display pyramid. Original measurements stay unchanged. Est. time: about 6 min Cancel Continue", f'Unexpected dialog content: {minimal}')
        report['shading_confirmation'] = minimal
'''
marker = '        # Snapshot only this private Agent WKWebView'
assert marker in source
source = source.replace(marker, orientation + extra + marker, 1)
dark_snapshot = '''        report['screenshots'] = {'light': str(snapshot)}
        dark_material = window.evaluate_js("document.documentElement.classList.remove('light'); document.querySelector('#frames iframe.active').contentDocument.documentElement.classList.remove('light'); getComputedStyle(document.getElementById('shading-confirm')).backgroundColor")
        check(dark_material == 'rgba(29, 29, 31, 0.82)', f'Dark glass material mismatch: {dark_material}')
        snapshot = session.exports_root / 'shading-dark.png'
        captured.clear()
        AppHelper.callAfter(lambda: BrowserView.instances[window.uid].webview.takeSnapshotWithConfiguration_completionHandler_(None, complete_snapshot))
        check(captured.wait(15), 'dark native snapshot timeout')
        report['screenshots']['dark'] = str(snapshot)
        window.evaluate_js('closeShadingConfirmation(false); shadingStates.set(active,window.__qaSavedShading); refreshShadingButton(); true')
        report['ok'] = True'''
source = source.replace("        report['ok'] = True", dark_snapshot, 1)
exec(compile(source, str(base), 'exec'))
