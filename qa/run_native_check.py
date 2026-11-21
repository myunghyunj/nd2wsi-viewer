"""Run one private Agent check and reap its own child after verified shutdown."""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
import time
import uuid
from pathlib import Path


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


def _resolve_target(target: Path, script: Path) -> tuple[list[str], str]:
    """Return (command, cwd) for either a Python interpreter or a dev-only .app bundle."""
    if target.is_dir() and (target / 'Contents/Info.plist').is_file():
        resolved = target.resolve()
        info = plistlib.loads((resolved / 'Contents/Info.plist').read_bytes())
        if not info.get('ND2WSIDevelopmentOnly', False):
            raise SystemExit(
                f'Refusing to run {script.name} via -c against production bundle {resolved}: '
                'hardened release apps reject arbitrary -c execution. Pass a Python interpreter '
                '(e.g. .venv/bin/python) or a development-only bundle (ND2WSIDevelopmentOnly=true).'
            )
        executable = resolved / 'Contents/MacOS' / info['CFBundleExecutable']
        code = f'__file__={str(script)!r}; exec(compile(open(__file__).read(),__file__,"exec"))'
        return [str(executable), '-c', code], '/private/tmp'
    if target.is_file() and os.access(target, os.X_OK):
        # Do NOT .resolve() Python interpreters: following .venv/bin/python's
        # symlink to the base interpreter drops the virtualenv's site-packages.
        return [os.path.abspath(target), str(script)], str(script.parent.parent)
    raise SystemExit(
        f'Unsupported native check target {target}: pass a Python executable '
        '(e.g. .venv/bin/python) or a development-only .app bundle.'
    )


def _reap_child(child: subprocess.Popen) -> bool:
    """Terminate and, if needed, kill a lingering child owned by this launcher."""
    if child.poll() is not None:
        return False
    child.terminate()
    try:
        child.wait(timeout=3)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('app', type=Path,
                        help='Python interpreter (e.g. .venv/bin/python) or development-only .app bundle')
    parser.add_argument('script', type=Path)
    parser.add_argument('--timeout', type=float, default=300.0,
                        help='Maximum seconds to wait before reaping a hung child (default: 300)')
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    qa_root = Path(__file__).resolve().parent
    script = args.script.resolve()
    if script.parent != qa_root or not script.name.startswith('check_native_'):
        parser.error('Use an identity-verified check_native_ script in this QA directory')
    target = Path(os.path.abspath(args.app))
    cmd, cwd = _resolve_target(target, script)
    # Every invocation has a fresh report, so an earlier session cannot qualify.
    report_path = qa_root / f'{script.stem}-{uuid.uuid4().hex}.json'
    env = dict(os.environ)
    env.pop('PYTHONPATH', None)
    env.update(ASTROMAP_QA_ROOT=str(qa_root), ASTROMAP_QA_REPORT=report_path.name)
    log_path = report_path.with_suffix('.log')
    child_reaped = False
    timed_out = False
    deadline = time.monotonic() + args.timeout
    with log_path.open('w') as log:
        child = subprocess.Popen(cmd, cwd=cwd, env=env,
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        while child.poll() is None:
            report = closed_agent(report_path, qa_root)
            if report is not None:
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    # The app has finished its server/window cleanup. Only reap the
                    # child created here; never discover or target a User window.
                    child_reaped = _reap_child(child)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                child_reaped = _reap_child(child)
                break
            time.sleep(.25)
    returncode = child.returncode
    report = closed_agent(report_path, qa_root)
    ok = bool(report and report.get('ok') and not timed_out)
    print(json.dumps({
        'report': str(report_path),
        'log': str(log_path),
        'ok': ok,
        'returncode': returncode,
        'child_reaped': child_reaped,
        'timed_out': timed_out,
    }))
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
