"""Run the JS tests' Node.js scripts through one persistent Node host per pytest process.

``run_node(script, *argv)`` behaves like
``subprocess.run([node, "-e", script, *argv], capture_output=True, text=True)``: the
script sees the same ``process.argv`` layout (``argv[1]`` is the first argument) and the
result is a ``subprocess.CompletedProcess`` with str ``stdout`` and ``stderr``. A new
Node process for every case is slow to start, on Windows above all, so each case runs in
a fresh ``worker_threads`` Worker inside one host process instead. A Worker has its own
module cache and globals, gets a copy of the caller's current environment and working
directory, and cannot change the host's; the host also restores its own environment and
directory after every case.

The host starts on first use and stops when the interpreter exits, so each pytest-xdist
worker gets its own. It also exits when its stdin closes, which covers a test process
that is killed outright. ``ND2WSI_NODE_SPAWN=1`` runs every case in its own
``node -e`` process instead, as the tests did before the host existed; it applies to
the whole session, for comparing the two transports.

A worker cannot call ``process.chdir``, and it shares the host's file descriptors: fd 0
is the request pipe, so a case gets no stdin, and fd 2 is the host's stderr. Bytes a case
writes straight to fd 1 do reach its stdout, but ahead of everything it wrote through
``process.stdout``. A script that needs any of this calls
``subprocess.run([NODE, "-e", ...])`` itself.
"""

from __future__ import annotations

import atexit
import collections
import json
import os
import queue
import secrets
import shutil
import subprocess
import threading
import time

import pytest

NODE = shutil.which("node")
SPAWN = os.environ.get("ND2WSI_NODE_SPAWN") == "1"
# How long past the case's own timeout to wait for the host to answer before
# treating the host itself as hung.
HOST_GRACE = 10.0

HOST_SCRIPT = r"""
// nd2wsi-node-host: runs each tests/node_host.py case in a fresh worker thread.
'use strict';
const {Worker} = require('worker_threads');
const readline = require('readline');
const util = require('util');
const baseEnv = {...process.env};
const baseCwd = process.cwd();
// Workers share this process's fd 1, so a case that writes to it directly
// (fs.writeSync(1, ...)) lands in the reply stream. Every reply line starts with
// this host's random mark; anything else on fd 1 is the current case's output.
const mark = process.argv[2];
const reply = (message) => process.stdout.write(mark + JSON.stringify(message) + '\n');
function restore() {
  for (const key of Object.keys(process.env)) if (!(key in baseEnv)) delete process.env[key];
  for (const [key, value] of Object.entries(baseEnv)) {
    if (process.env[key] !== value) process.env[key] = value;
  }
  if (process.cwd() !== baseCwd) process.chdir(baseCwd);
}
// `node -e` prints an uncaught string as is and anything else inspected, which
// keeps an Error's own properties (an AssertionError's actual and expected).
const describe = (error) => (typeof error === 'string' ? error : util.inspect(error)) + '\n';
// The worker runs this fixed bootstrap and the case script arrives untouched in
// workerData, so a 'use strict' directive stays a directive. Like `node -e SCRIPT a b`,
// the script sees process.argv = [node, a, b], the globals require, module and exports,
// and '[eval]' as its file name in stack traces. The arrow function keeps the
// bootstrap's own names out of the script's global scope.
const BOOTSTRAP = `(() => {
  const {workerData} = require('worker_threads');
  const vm = require('vm');
  process.argv = workerData.argv;
  globalThis.__filename = '[eval]';
  const options = {filename: '[eval]'};
  if (vm.constants && vm.constants.USE_MAIN_CONTEXT_DEFAULT_LOADER) {
    options.importModuleDynamically = vm.constants.USE_MAIN_CONTEXT_DEFAULT_LOADER;
  }
  vm.runInThisContext(workerData.script, options);
})();`;
function runCase(request) {
  return new Promise((resolve) => {
    if (request.cwd && request.cwd !== process.cwd()) {
      try { process.chdir(request.cwd); } catch (error) { /* keep the host's directory */ }
    }
    const out = [], err = [];
    let worker;
    try {
      worker = new Worker(BOOTSTRAP, {
        eval: true, stdout: true, stderr: true,
        env: request.env || process.env,
        workerData: {script: request.script, argv: [process.execPath, ...request.argv]},
      });
    } catch (error) {
      restore();
      reply({id: request.id, code: 1, stdout: '', stderr: describe(error)});
      resolve();
      return;
    }
    let streamsOpen = 2, exitCode = null, timedOut = false, done = false, timer = null, drain = null;
    const finish = (note) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      clearTimeout(drain);
      restore();
      if (note) err.push(Buffer.from(note));
      reply({
        id: request.id, code: exitCode, timedOut,
        stdout: Buffer.concat(out).toString('utf8'),
        stderr: Buffer.concat(err).toString('utf8'),
      });
      resolve();
    };
    const settle = () => { if (exitCode !== null && streamsOpen === 0) finish(); };
    worker.stdout.on('data', (chunk) => out.push(chunk));
    worker.stderr.on('data', (chunk) => err.push(chunk));
    worker.stdout.on('end', () => { streamsOpen--; settle(); });
    worker.stderr.on('end', () => { streamsOpen--; settle(); });
    // `node -e` prints an uncaught exception to stderr; a worker reports it here.
    worker.on('error', (error) => err.push(Buffer.from(describe(error))));
    worker.on('exit', (code) => {
      exitCode = code;
      clearTimeout(timer);
      settle();
      // Both streams normally end with the thread; never let a stuck stream hold the host.
      if (!done) drain = setTimeout(() => finish('[node host] worker output did not end after exit\n'), 2000);
    });
    if (request.timeoutMs != null) {
      timer = setTimeout(() => { timedOut = true; worker.terminate(); }, request.timeoutMs);
    }
  });
}
let chain = Promise.resolve();
const input = readline.createInterface({input: process.stdin});
input.on('line', (line) => {
  let request;
  try {
    request = JSON.parse(line);
  } catch (error) {
    reply({id: null, protocolError: String(error)});
    return;
  }
  chain = chain.then(() => runCase(request));
});
// The test process has gone: stop, along with any worker still running.
input.on('close', () => process.exit(0));
process.stdout.on('error', () => process.exit(0));
"""


class NodeHostError(RuntimeError):
    """The host process died or broke protocol while running a case."""


class NodeScriptError(subprocess.CalledProcessError):
    """``check=True`` and the script exited with a non-zero status."""

    def __str__(self):
        return f"node script exited with status {self.returncode}\n{_tail(self.stderr)}"


class NodeTimeout(subprocess.TimeoutExpired):
    """The script ran longer than its timeout and was stopped."""

    def __str__(self):
        return f"node script timed out after {self.timeout} seconds\n{_tail(self.stderr)}"


def _tail(text, limit=4000):
    text = text or ""
    return text if len(text) <= limit else "...\n" + text[-limit:]


def _text(value):
    # subprocess.run(text=True) decodes and translates newlines the same way.
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _cwd():
    try:
        return os.getcwd()
    except OSError:
        return None


class _Host:
    def __init__(self):
        self.owner = os.getpid()
        self.next_id = 0
        self.replies = queue.SimpleQueue()
        self.stderr = collections.deque(maxlen=100)
        # Starts every reply line, so output a case writes straight to fd 1 cannot
        # pass for a reply.
        self.mark = f"#{secrets.token_hex(16)}#"
        self.proc = subprocess.Popen(
            [NODE, "-e", HOST_SCRIPT, "nd2wsi-node-host", self.mark],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.readers = [
            threading.Thread(target=self._read_replies, name="node-host-stdout", daemon=True),
            threading.Thread(target=self._read_stderr, name="node-host-stderr", daemon=True),
        ]
        for reader in self.readers:
            reader.start()

    # Each reader closes its own pipe at the end of the stream: closing it from another
    # thread would wait for a read in progress.
    def _read_replies(self):
        try:
            for line in self.proc.stdout:
                self.replies.put(line)
        except (OSError, ValueError):
            pass
        finally:
            _close(self.proc.stdout)
        self.replies.put(None)

    def _read_stderr(self):
        try:
            for line in self.proc.stderr:
                self.stderr.append(line.decode("utf-8", "replace"))
        except (OSError, ValueError):
            pass
        finally:
            _close(self.proc.stderr)

    def alive(self):
        return self.owner == os.getpid() and self.proc.poll() is None

    def failure(self, what):
        self.stop()
        detail = "".join(self.stderr).strip()
        return NodeHostError(
            f"node host {what} (exit status {self.proc.returncode}); the next case starts "
            f"a new host." + (f"\nhost stderr:\n{_tail(detail)}" if detail else "")
        )

    def run(self, args, script, argv, timeout):
        self.next_id += 1
        # A spawned node would inherit the caller's environment and directory at call time.
        request = {"id": self.next_id, "script": script, "argv": argv,
                   "timeoutMs": None if timeout is None else int(timeout * 1000),
                   "env": dict(os.environ), "cwd": _cwd()}
        try:
            self.proc.stdin.write(json.dumps(request).encode("ascii") + b"\n")
            self.proc.stdin.flush()
        except OSError:
            raise self.failure("exited before it received this case") from None
        deadline = None if timeout is None else time.monotonic() + timeout + HOST_GRACE
        mark = self.mark.encode("ascii")
        stray = []
        while True:
            wait = None if deadline is None else max(0.0, deadline - time.monotonic())
            try:
                line = self.replies.get(timeout=wait)
            except queue.Empty:
                self.stop()
                raise NodeTimeout(args, timeout, "", "node host stopped answering; it was "
                                  "killed and the next case starts a new host") from None
            if line is None:
                raise self.failure("exited while running this case")
            # Bytes the case wrote to fd 1 itself arrive before its reply, which is sent
            # once the worker has exited; a `node -e` process would print them on stdout.
            start = line.find(mark)
            if start < 0:
                stray.append(line)
                continue
            stray.append(line[:start])
            try:
                message = json.loads(line[start + len(mark):].decode("utf-8"))
            except ValueError:
                raise self.failure(f"wrote a reply that is not JSON: {line[:200]!r}") from None
            if "protocolError" in message:
                raise self.failure(f"could not read a request: {message['protocolError']}")
            if message.get("id") == request["id"]:
                message["stdout"] = b"".join(stray).decode("utf-8", "replace") + message["stdout"]
                return message
            stray = []

    def stop(self):
        if self.owner != os.getpid():
            return
        # Closing stdin asks a running host to exit.
        _close(self.proc.stdin)
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        # Once the process has gone, the readers reach the end and close their pipes.
        for reader in self.readers:
            reader.join(timeout=2)


def _close(pipe):
    try:
        pipe.close()
    except (OSError, ValueError):
        pass


_host = None
_lock = threading.Lock()


def _shutdown():
    if _host is not None:
        _host.stop()


atexit.register(_shutdown)


def _host_run(args, script, argv, timeout):
    global _host
    with _lock:
        if _host is None or not _host.alive():
            _host = _Host()
        try:
            return _host.run(args, script, argv, timeout)
        except BaseException:
            # A failed or interrupted case leaves the host in an unknown state.
            if not _host.alive():
                _host = None
            raise


def run_node(script, *argv, timeout=20, check=False):
    """Run ``node -e script argv...`` and return a ``subprocess.CompletedProcess``.

    stdout and stderr are str decoded as UTF-8. A missing ``node`` skips the test. A
    timeout raises ``subprocess.TimeoutExpired`` and ``check=True`` raises
    ``subprocess.CalledProcessError`` for a non-zero status, as ``subprocess.run`` does.
    """
    if NODE is None:
        pytest.skip("node is not installed")
    argv = [str(arg) for arg in argv]
    args = [NODE, "-e", script, *argv]
    if SPAWN:
        try:
            result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise NodeTimeout(args, timeout, _decode(exc.stdout), _decode(exc.stderr)) from None
        code, stdout, stderr = result.returncode, result.stdout, result.stderr
    else:
        message = _host_run(args, script, argv, timeout)
        stdout, stderr = _text(message["stdout"]), _text(message["stderr"])
        if message.get("timedOut"):
            raise NodeTimeout(args, timeout, stdout, stderr)
        code = message["code"]
    if check and code:
        raise NodeScriptError(code, args, stdout, stderr)
    return subprocess.CompletedProcess(args, code, stdout, stderr)


def _decode(value):
    if isinstance(value, bytes):
        return _text(value.decode("utf-8", "replace"))
    return value or ""
