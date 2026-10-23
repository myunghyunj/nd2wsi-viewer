"""Native star and annotation-orientation checks in a fresh Agent window."""
from pathlib import Path

base = Path(__file__).with_name('check_native_astromap.py')
source = base.read_text()
extra = '''        def pane(code):
            return window.evaluate_js("document.querySelector('#frames iframe.active').contentWindow.eval("+json.dumps(code)+")")
        def wait_pane(code):
            return wait_js("document.querySelector('#frames iframe.active').contentWindow.eval("+json.dumps(code)+")")
        pane("state.viewportRelay.displayRotation=0; state.viewportRelay.displayFlipped=false; applyDesiredDisplayTransform(); state.viewer.viewport.goHome(true); addAnnotation({type:'pin',x:1100,y:1200,text:'Pin follows image'}); addAnnotation({type:'line',x1:1000,y1:1200,x2:1500,y2:1450,text:'Line follows image'}); true")
        pane("document.activeElement?.blur(); renderAnnotations(); true")
        before=pane('JSON.stringify(state.annotations)')
        geometry=r"""(() => {
          const root=document.getElementById('ann-layer'), points=[];
          for(const p of root.querySelectorAll('g.hit > polygon'))
            for(const xy of p.getAttribute('points').trim().split(/\\s+/)) points.push(xy.split(',').map(Number));
          for(const g of root.querySelectorAll('g.hit')) {
            const p=g.querySelector('circle'); if(p) points.push([+p.getAttribute('cx'),+p.getAttribute('cy')]);
          }
          for(const g of root.children) {
            const line=g.querySelector('line'); if(line) points.push([+line.getAttribute('x1'),+line.getAttribute('y1')],[+line.getAttribute('x2'),+line.getAttribute('y2')]);
          }
          return {points,size:[state.viewer.viewport.getContainerSize().x,state.viewer.viewport.getContainerSize().y],rotation:state.viewer.viewport.getRotation(),flip:state.viewer.viewport.getFlip()};
        })()"""
        original=pane(geometry)
        check(len(original['points'])==11, f'Expected 2 boxes, pin, measurement: {original}')
        measured=[]
        last=original
        for letter,origin in [('f','pane'),('f','shell'),('t','pane'),('f','pane'),('t','shell')]:
            event="document.dispatchEvent(new KeyboardEvent('keydown',{key:"+json.dumps(letter)+",code:"+json.dumps('Key'+letter.upper())+",metaKey:true,bubbles:true,cancelable:true})); true"
            (pane if origin=='pane' else window.evaluate_js)(event)
            if letter=='f':
                wait_pane('state.viewer.viewport.getFlip() === '+str(not last['flip']).lower())
            else:
                expected_rotation=(last['rotation']+(-90 if last['flip'] else 90))%360
                wait_pane('Math.abs(state.viewer.viewport.getRotation()-'+str(expected_rotation)+')<0.01')
            now=pane(geometry)
            width,height=last['size']
            if letter=='f':
                expected=[[width-x,y] for x,y in last['points']]
            else:
                expected=[[width/2-(y-height/2),height/2+(x-width/2)] for x,y in last['points']]
            error=max(abs(actual-want) for p,q in zip(now['points'],expected) for actual,want in zip(p,q))
            check(error<1.0, f'Annotation {letter} display mismatch {error}px: {now}')
            check(pane('JSON.stringify(state.annotations)')==before,'Orientation mutated source annotation coordinates')
            measured.append({'key':'Cmd+'+letter.upper(),'origin':origin,'max_error_px':error,'rotation':now['rotation'],'flipped':now['flip']})
            last=now
        report['annotation_orientation']=measured
        report['annotation_source_coordinates_unchanged']=True
        pane('state.viewportRelay.displayRotation=0; state.viewportRelay.displayFlipped=false; applyDesiredDisplayTransform(); true')
'''
marker='        # Snapshot only this private Agent WKWebView'
assert marker in source
source=source.replace(marker,extra+marker,1)
exec(compile(source,str(base),'exec'))
