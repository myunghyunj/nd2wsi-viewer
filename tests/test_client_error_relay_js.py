"""Shell and pane script errors reach the native session-log bridge, and nothing else."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "nd2wsi" / "static"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SCRIPT = r"""
const fs = require('fs'), vm = require('vm');
const root = process.argv[1], origin = 'http://127.0.0.1:5123';
const shellSource = fs.readFileSync(root + '/shell-v1.js', 'utf8');
const appSource = fs.readFileSync(root + '/app.js', 'utf8');
function between(source, start, end) {
  const from = source.indexOf(start), to = source.indexOf(end, from);
  if (from < 0 || to < 0) throw Error('Missing production code: ' + start);
  return source.slice(from, to);
}
function production(source, name) {
  const match = new RegExp('^function ' + name + '\\(', 'm').exec(source);
  if (!match) throw Error('Missing production function: ' + name);
  return source.slice(match.index, source.indexOf('\n}', match.index) + 2);
}
let unhandled = 0;
process.on('unhandledRejection', () => { unhandled++; });
const clock = {now: 1000};
const FakeDate = {now: () => clock.now};
function fakeWindow(extra = {}) {
  const listeners = new Map();
  return {listeners, ...extra, addEventListener(kind, callback) {
    if (!listeners.has(kind)) listeners.set(kind, []);
    listeners.get(kind).push(callback);
  }, emit(kind, event) {
    for (const callback of listeners.get(kind) || []) callback(event);
  }};
}

const calls = [];
const shellWindow = fakeWindow();
const bridge = {log_client_event(payload) { calls.push(payload); return Promise.resolve({ok:true}); }};
const frames = new Map();
const shell = vm.createContext({window:shellWindow, location:{origin}, frames, Date:FakeDate,
  VIEWPORT_PROTOCOL_VERSION:2});
vm.runInContext(between(shellSource, 'const clientErrors = {', 'let metalOpening = false;'), shell);
vm.runInContext(production(shellSource, 'frameSidForSource'), shell);
const message = shellSource.indexOf('window.addEventListener("message", (event) => {');
vm.runInContext(shellSource.slice(message, shellSource.indexOf('\n});', message) + 4), shell);

const posted = [];
function makePane(sid, {standalone = false, failPost = false} = {}) {
  const pane = fakeWindow();
  pane.parent = standalone ? pane : {postMessage(data, target) {
    if (failPost) throw Error('detached frame');
    posted.push({sid, data, target});
    shellWindow.emit('message', {origin, source:pane, data});
  }};
  if (!standalone) frames.set(sid, {contentWindow:pane});
  const context = vm.createContext({window:pane, location:{origin}, Date:FakeDate});
  vm.runInContext(between(appSource, 'const paneErrorsSent = [];',
    'window.Nd2ShortcutRouter?.localizeLabels(document);'), context);
  return {pane, context};
}
const errorEvent = (message, error, line = 7) => ({message, error,
  filename: origin + '/token/static/app.js', lineno: line, colno: 3});

Promise.resolve(vm.runInContext('(async()=>{' + process.argv[2] + '})()', vm.createContext({
  shell, shellWindow, bridge, calls, posted, makePane, errorEvent, clock, frames, origin,
  unhandled: () => unhandled, Error, Promise, JSON,
}))).then(async result => {
  for (let turn = 0; turn < 5; turn++) await Promise.resolve();
  process.stdout.write(JSON.stringify({result, unhandled}));
}).catch(error => { process.stderr.write(String(error.stack || error)); process.exitCode = 1; });
"""


def run(body):
    result = subprocess.run([NODE, "-e", SCRIPT, str(STATIC), body],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_shell_errors_and_rejections_reach_the_bridge_capped_and_never_throw():
    out = run("""
      shellWindow.pywebview = {api: bridge};
      const error = new Error('shell boom');
      shellWindow.emit('error', {message:'Uncaught Error: shell boom', error,
        filename:origin + '/token/static/shell-v1.js', lineno:12, colno:4});
      shellWindow.emit('unhandledrejection', {reason:new Error('x'.repeat(5000))});
      shellWindow.emit('unhandledrejection', {reason:'plain reason'});
      shellWindow.emit('unhandledrejection', {reason:{toString() { throw Error('no text'); }}});
      shellWindow.emit('unhandledrejection', {reason:undefined});
      bridge.log_client_event = () => { throw Error('bridge gone'); };
      shellWindow.emit('error', {message:'second', error:null});
      bridge.log_client_event = () => Promise.reject(new Error('Python raised'));
      shellWindow.emit('error', {message:'third', error:null});
      return calls.map(call => ({...call, stack:call.stack.length ? 'stack' : '', message:call.message.slice(0, 40),
        length:call.message.length}));
    """)
    assert out["unhandled"] == 0
    first, long, plain, unprintable, empty = out["result"]
    assert first == {"source": "shell", "kind": "error", "message": "Uncaught Error: shell boom",
                     "where": "http://127.0.0.1:5123/token/static/shell-v1.js:12:4", "stack": "stack",
                     "length": len("Uncaught Error: shell boom")}
    assert long["kind"] == "unhandledrejection" and long["length"] == 2000
    assert plain["message"] == "plain reason" and plain["where"] == "" and plain["stack"] == ""
    assert unprintable["message"] == "[unprintable]"
    assert empty["message"] == ""


def test_shell_holds_early_reports_until_the_bridge_is_ready_then_rate_limits():
    out = run("""
      for (let index = 0; index < 12; index++) shellWindow.emit('error', {message:'early ' + index, error:null});
      const beforeReady = calls.length;
      shellWindow.pywebview = {api: bridge};
      shellWindow.emit('pywebviewready', {});
      const flushed = calls.map(call => call.message);
      for (let index = 0; index < 12; index++) shellWindow.emit('error', {message:'late ' + index, error:null});
      const limited = calls.length;
      clock.now += 60000;
      shellWindow.emit('error', {message:'next minute', error:null});
      return {beforeReady, flushed, limited, last:calls[calls.length - 1].message};
    """)
    result = out["result"]
    assert result["beforeReady"] == 0
    # Ten are held; all twelve count toward the twenty a minute.
    assert result["flushed"] == [f"early {index}" for index in range(10)]
    assert result["limited"] == 10 + 8
    assert result["last"] == "next minute"


def test_pane_errors_go_to_the_shell_over_the_scoped_channel_and_on_to_the_bridge():
    out = run("""
      shellWindow.pywebview = {api: bridge};
      const {pane} = makePane('a');
      pane.emit('error', errorEvent('Uncaught TypeError: pane boom', new TypeError('pane boom')));
      pane.emit('unhandledrejection', {reason:new Error('tile request')});
      return {posted:posted.map(item => ({target:item.target, ...item.data, stack:!!item.data.stack})), calls};
    """)
    posted, calls = out["result"]["posted"], out["result"]["calls"]
    assert [item["target"] for item in posted] == ["http://127.0.0.1:5123"] * 2
    assert posted[0] == {"target": "http://127.0.0.1:5123", "nd2wsi": "client-error", "kind": "error",
                         "message": "Uncaught TypeError: pane boom",
                         "where": "http://127.0.0.1:5123/token/static/app.js:7:3", "stack": True}
    assert [(call["source"], call["kind"], call["message"]) for call in calls] == [
        ("pane", "error", "Uncaught TypeError: pane boom"),
        ("pane", "unhandledrejection", "tile request"),
    ]


def test_the_shell_ignores_reports_from_other_origins_or_windows():
    out = run("""
      shellWindow.pywebview = {api: bridge};
      makePane('a');
      const data = {nd2wsi:'client-error', kind:'error', message:'forged', where:'', stack:''};
      shellWindow.emit('message', {origin:'http://evil.invalid', source:frames.get('a').contentWindow, data});
      shellWindow.emit('message', {origin, source:{}, data});
      return calls.length;
    """)
    assert out["result"] == 0


def test_panes_rate_limit_and_never_throw_or_report_when_standalone():
    out = run("""
      shellWindow.pywebview = {api: bridge};
      const {pane} = makePane('a');
      for (let index = 0; index < 25; index++) pane.emit('error', errorEvent('pane ' + index, null));
      const limited = posted.length;
      clock.now += 60000;
      pane.emit('error', errorEvent('pane later', null));
      const alone = makePane('solo', {standalone:true});
      alone.pane.emit('error', errorEvent('standalone', null));
      const detached = makePane('gone', {failPost:true});
      const relayed = detached.context.relayPaneError('error', new Error('detached'));
      detached.pane.emit('unhandledrejection', {reason:{get message() { throw Error('trap'); }}});
      return {limited, total:posted.length, last:posted[posted.length - 1].data.message,
        sources:[...new Set(posted.map(item => item.sid))], relayed};
    """)
    result = out["result"]
    assert result["limited"] == 20
    assert result["total"] == 21 and result["last"] == "pane later"
    assert result["sources"] == ["a"]
    assert result["relayed"] is False
