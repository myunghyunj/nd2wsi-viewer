"""Regression tests for 2.6.7 shell focus-stealing guard and shell-compare extraction."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from node_host import NODE, run_node

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "nd2wsi" / "static"

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

FOCUS_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1] + '/shell-v1.js', 'utf8');
const ShortcutRouter = require(process.argv[1] + '/shortcut-router-v1.js');
const cfg = JSON.parse(process.argv[2]);
const focused = [];
const messages = [];
const body = { tagName: 'BODY' };
const document = {
  body,
  documentElement: { tagName: 'HTML' },
  activeElement: body,
};
if (cfg.hasFocus !== 'absent') {
  document.hasFocus = () => cfg.hasFocus;
}
const frames = new Map();
const readyFrames = new Set(['a', 'b']);
for (const sid of ['a', 'b']) {
  const frame = {
    tagName: 'IFRAME',
    dataset: { sid },
    contentWindow: {
      focus() { focused.push(sid + ':window'); },
      postMessage(data) { messages.push(data); },
    },
    focus(opts) {
      document.activeElement = frame;
      focused.push(sid + (opts && opts.preventScroll ? ':preventScroll' : ''));
    },
  };
  frames.set(sid, frame);
}
const context = vm.createContext({
  document,
  frames,
  readyFrames,
  ShortcutRouter,
  focused,
  messages,
  active: 'a',
  location: { origin: 'http://qa.invalid' },
  quitPreparation: null,
  refreshAstromapButton() {},
  refreshShellToolButtons() {},
  pairPicker: { open: false },
  compare: { enabled: false, members: ['b'] },
  closePairPicker() {},
  rememberSlide() {},
  ensureFrame() {},
  applyFrameLayout() {},
  render() {},
  scheduleNativeGestureScopes() {},
  refreshShadingButton() {},
  inGroup: sid => ['a', 'b'].includes(sid),
  broadcastCompareState() {},
  spatialGroupReady: () => false,
  sendTabShortcutState() {},
  groupSids: () => ['a', 'b'],
  setLinkMenuVisible() {},
  rememberAlignment() {},
  invalidateSpatialWork() {},
  clearDisplayTransforms() {},
  updateCompareControls() {},
});
for (const name of ['focusActivePane', 'activate', 'paneCameUp', 'stopCompare']) {
  const at = source.indexOf('function ' + name + '(');
  if (at < 0) throw new Error('missing function: ' + name);
  vm.runInContext(source.slice(at, source.indexOf('\n}', at) + 2), context);
}
const out = vm.runInContext(process.argv[3], context);
process.stdout.write(JSON.stringify(out));
"""


def _run_focus(has_focus: bool | str, body: str) -> dict:
    result = run_node(FOCUS_HARNESS, STATIC, json.dumps({"hasFocus": has_focus}), body, check=True)
    return json.loads(result.stdout)


def test_focus_active_pane_does_not_steal_focus_when_document_unfocused():
    out = _run_focus(
        False,
        """
        const immediate = focusActivePane(false);
        const deferred = focusActivePane(true);
        paneCameUp('a');
        activate('b');
        compare.enabled = true;
        stopCompare();
        ({
          immediate,
          deferred,
          focused,
          activeTag: document.activeElement.tagName,
        });
        """,
    )
    assert out == {
        "immediate": False,
        "deferred": False,
        "focused": [],
        "activeTag": "BODY",
    }


def test_focus_active_pane_focuses_frame_when_document_has_focus():
    out = _run_focus(
        True,
        """
        const immediate = focusActivePane(false);
        const focusedAfterImmediate = focused.slice();
        document.activeElement = document.body;
        paneCameUp('a');
        const focusedAfterDeferred = focused.slice();
        activate('b');
        ({
          immediate,
          focusedAfterImmediate,
          focusedAfterDeferred,
          focused,
          targetSid: document.activeElement.dataset.sid,
        });
        """,
    )
    assert out == {
        "immediate": True,
        "focusedAfterImmediate": ["a:preventScroll", "a:window"],
        "focusedAfterDeferred": [
            "a:preventScroll",
            "a:window",
            "a:preventScroll",
            "a:window",
        ],
        "focused": [
            "a:preventScroll",
            "a:window",
            "a:preventScroll",
            "a:window",
            "b:preventScroll",
            "b:window",
        ],
        "targetSid": "b",
    }


def test_focus_active_pane_works_when_document_has_focus_is_undefined():
    out = _run_focus(
        "absent",
        """
        const ok = focusActivePane();
        ({ ok, focused, targetSid: document.activeElement.dataset.sid });
        """,
    )
    assert out == {
        "ok": True,
        "focused": ["a:preventScroll", "a:window"],
        "targetSid": "a",
    }


def test_shell_compare_module_wired_in_shell_html_and_parses_cleanly():
    shell_html = (STATIC / "shell.html").read_text(encoding="utf-8")
    shell_js = (STATIC / "shell-v1.js").read_text(encoding="utf-8")
    compare_js = STATIC / "shell-compare-v1.js"
    assert compare_js.is_file()

    compare_tag = '<script src="static/shell-compare-v1.js"></script>'
    shell_tag = '<script src="static/shell-v1.js"></script>'
    assert compare_tag in shell_html
    assert shell_tag in shell_html
    assert shell_html.index('src="static/align-v1.js"') < shell_html.index(compare_tag)
    assert shell_html.index('src="static/update-card-v1.js"') < shell_html.index(compare_tag)
    assert shell_html.index(compare_tag) < shell_html.index(shell_tag)
    assert "window.ND2ShellCompare.createCompare(" in shell_js

    for js_file in (compare_js, STATIC / "shell-v1.js"):
        proc = subprocess.run(
            [NODE, "--check", str(js_file)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"node --check failed for {js_file.name}: {proc.stderr}"

    # Verify browser global registration (without CommonJS module/exports)
    probe = r"""
    const fs = require('fs');
    const vm = require('vm');
    const src = fs.readFileSync(process.argv[1] + '/shell-compare-v1.js', 'utf8');
    const sandbox = { globalThis: null };
    sandbox.globalThis = sandbox;
    vm.runInNewContext(src, sandbox);
    const mod = sandbox.ND2ShellCompare;
    const api = mod.createCompare({
      compare: { enabled: false, members: [], states: new Map(), pairs: new Map(),
        memory: new Map(), mru: [], landmark: { active: false, edit: null },
        routeTimers: new Map(), routeLatest: new Map(), nudgeQueue: [],
        retiredInstances: new Map(), anchorSet: { id: null, revision: 0, points: [] },
        anchorLandmarks: [] },
      pairPicker: { open: false, mode: 'start', replaceSid: null },
      frames: new Map(),
      getSlides: () => [],
      getActive: () => null,
      setActive() {},
      Align: require(process.argv[1] + '/align-v1.js'),
      ShortcutRouter: require(process.argv[1] + '/shortcut-router-v1.js'),
      $: () => null,
      groupSids: () => [],
      inGroup: () => false,
      slideName: () => 'Slide',
      rememberSlide() {},
      ensureFrame() {},
      applyFrameLayout() {},
      render() {},
      stopCompare() {},
      postToSlide: () => false,
      showError() {},
      scheduleNativeGestureScopes() {},
      finitePoint: () => null,
      localIdentity: () => null,
      sameIdentity: () => false,
    });
    process.stdout.write(JSON.stringify({
      hasFactory: typeof mod.createCompare === 'function',
      hasActions: Object.keys(mod.ORIENTATION_ACTIONS).length,
      methodCount: Object.keys(api).length,
      hasWire: typeof api.wireCompareInteractions === 'function',
      pose: api.displayTransformFor('a'),
    }));
    """
    res = json.loads(run_node(probe, STATIC, check=True).stdout)
    assert res["hasFactory"] is True
    assert res["hasActions"] == 6
    assert res["methodCount"] >= 90
    assert res["hasWire"] is True
    assert res["pose"] == {"degrees": 0, "flipped": False}
