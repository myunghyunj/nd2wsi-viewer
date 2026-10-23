"""Run one private Agent check and reap its own child after verified shutdown."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
import time
import uuid


def closed_agent(report_path, qa_root):
    try:
        report = json.loads(report_path.read_text())
        context = report['session']
        root = Path(context['root']).resolve()
        if (context['role'] != 'agent' or root.parent != qa_root / 'sessions'
                or root.name != context['id']):
            return None
        saved = json.loads((root / 'session.json').read_text())
        if (saved.get('id') == context['id'] and saved.get('role') == 'agent'
                and saved.get('state') == 'closed'):
            return report
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('app', type=Path)
    parser.add_argument('script', type=Path)
    args = parser.parse_args()
    qa_root = Path(__file__).resolve().parent
    script = args.script.resolve()
    if script.parent != qa_root or not script.name.startswith('check_native_'):
        parser.error('Use an identity-verified check_native_ script in this QA directory')
    app = args.app.resolve()
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    executable = app / 'Contents/MacOS' / info['CFBundleExecutable']
    # Every invocation has a fresh report, so an earlier session cannot qualify.
    report_path = qa_root / f'{script.stem}-{uuid.uuid4().hex}.json'
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    env.update(ASTROMAP_QA_ROOT=str(qa_root), ASTROMAP_QA_REPORT=report_path.name)
    code = f'__file__={str(script)!r}; exec(compile(open(__file__).read(),__file__,"exec"))'
    log_path = report_path.with_suffix('.log')
    with log_path.open('w') as log:
        child = subprocess.Popen([str(executable), '-c', code], cwd='/private/tmp',
                                 env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        while child.poll() is None:
            report = closed_agent(report_path, qa_root)
            if report is None:
                time.sleep(.25)
                continue
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                # The app has finished its server/window cleanup. Only reap the
                # child created here; never discover or target a User window.
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            break
    report = closed_agent(report_path, qa_root)
    print(json.dumps({'report': str(report_path), 'log': str(log_path),
                      'ok': bool(report and report.get('ok')), 'child_reaped': True}))
    return 0 if report and report.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
