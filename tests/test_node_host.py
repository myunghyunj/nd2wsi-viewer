"""The JS tests' Node transport: a persistent host with a fresh worker per case.

Every contract case runs through the host and through the ND2WSI_NODE_SPAWN=1
fallback, which must agree with each other and with `node -e SCRIPT ARGS...`.
"""

import json
import subprocess
from pathlib import Path

import node_host
import pytest
from node_host import run_node

MODULE = Path(__file__).resolve().parents[1] / "nd2wsi" / "static" / "align-v1.js"


@pytest.fixture(params=["host", "spawn"])
def transport(request, monkeypatch):
    monkeypatch.setattr(node_host, "SPAWN", request.param == "spawn")
    return request.param


@pytest.fixture
def host(monkeypatch):
    monkeypatch.setattr(node_host, "SPAWN", False)


def test_arguments_follow_the_node_e_layout_as_utf8_text(transport):
    result = run_node(
        "process.stdout.write(JSON.stringify(process.argv.slice(1)) + '\\r\\n한글');",
        "plain", MODULE, '{"a": "é"}',
    )
    assert result.returncode == 0 and result.stderr == ""
    first, second = result.stdout.split("\n")
    assert json.loads(first) == ["plain", str(MODULE), '{"a": "é"}']
    assert second == "한글"


@pytest.mark.parametrize("script,code,message", [
    ("process.exitCode = 3; process.stdout.write('out');", 3, ""),
    ("console.log('out'); process.exit(4);", 4, ""),
    ("process.stdout.write('out');\nthrow new Error('boom');", 1, "Error: boom"),
    ("process.stdout.write('out'); Promise.reject(new Error('later'));", 1, "Error: later"),
])
def test_exit_status_output_and_uncaught_errors_match_a_node_process(transport, script, code, message):
    result = run_node(script)
    assert result.returncode == code
    assert result.stdout.startswith("out")
    assert message in result.stderr


def test_an_uncaught_value_that_is_not_an_error_is_shown_inspected(transport):
    result = run_node("throw {status: 500, body: 'down'};")
    assert result.returncode == 1
    assert "{ status: 500, body: 'down' }" in result.stderr


def test_stack_traces_name_the_script_eval_with_its_own_line_numbers(transport):
    result = run_node("process.stdout.write('out');\nnull.missing;")
    assert result.returncode == 1
    assert "[eval]:2" in result.stderr
    assert "process.argv" not in result.stderr


@pytest.mark.parametrize("script", [
    "'use strict'\n",
    "'use strict';\n",
    "// a header comment\n\"use strict\";\n",
])
def test_a_use_strict_directive_applies_and_the_script_owns_its_global_names(transport, script):
    result = run_node(
        script + "const vm = 1, workerData = 2, options = 3;\n"
        "process.stdout.write(JSON.stringify([(function () { return this; })() === undefined,"
        " vm + workerData + options, process.argv.slice(1)]));",
        "arg",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [True, 6, ["arg"]]


def test_output_written_straight_to_fd_1_is_the_scripts_stdout(transport):
    # Workers share the host's fd 1 with its replies.
    result = run_node(
        "const fs = require('fs'); fs.writeSync(1, 'line\\n'); fs.writeSync(1, 'partial');"
        " process.stdout.write('ok');"
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, "line\npartialok", "")
    assert run_node("process.stdout.write('next')").stdout == "next"


def test_check_raises_called_process_error_with_the_script_stderr(transport):
    with pytest.raises(subprocess.CalledProcessError) as caught:
        run_node("console.error('reason'); process.exit(2);", check=True)
    assert caught.value.returncode == 2
    assert caught.value.stderr == "reason\n"
    assert "reason" in str(caught.value)


def test_a_timeout_stops_the_case_and_the_next_case_still_runs(transport):
    with pytest.raises(subprocess.TimeoutExpired):
        run_node("process.stdout.write('started'); while (true) {}", timeout=0.5)
    assert run_node("process.stdout.write('next')").stdout == "next"


def test_globals_environment_and_module_cache_never_leak_between_cases(transport):
    run_node(
        "globalThis.leaked = 1; process.env.ND2WSI_LEAK = '1';"
        "require(process.argv[1]).leaked = 1;",
        MODULE, check=True,
    )
    result = run_node(
        "process.stdout.write(JSON.stringify([typeof leaked, process.env.ND2WSI_LEAK || null,"
        " require(process.argv[1]).leaked || null]));",
        MODULE, check=True,
    )
    assert json.loads(result.stdout) == ["undefined", None, None]


def test_each_case_sees_the_callers_current_environment(transport, monkeypatch):
    monkeypatch.setenv("ND2WSI_NODE_HOST_PROBE", "first")
    assert run_node("process.stdout.write(process.env.ND2WSI_NODE_HOST_PROBE)").stdout == "first"
    monkeypatch.setenv("ND2WSI_NODE_HOST_PROBE", "second")
    assert run_node("process.stdout.write(process.env.ND2WSI_NODE_HOST_PROBE)").stdout == "second"


def test_large_output_arrives_whole(transport):
    result = run_node("process.stdout.write('x'.repeat(4 * 1024 * 1024));")
    assert result.returncode == 0 and len(result.stdout) == 4 * 1024 * 1024


def test_the_host_waits_for_all_output_when_a_script_exits_at_once(host):
    # A node process on a macOS pipe can drop the tail here; the host reads to the end.
    result = run_node("process.stdout.write('x'.repeat(4 * 1024 * 1024)); process.exit(5);")
    assert result.returncode == 5 and len(result.stdout) == 4 * 1024 * 1024


def test_a_crashed_host_fails_the_case_and_the_next_case_gets_a_new_host(host):
    run_node("")
    crashed = node_host._host
    with pytest.raises(node_host.NodeHostError, match="exited while running this case"):
        run_node("process.kill(process.pid, 'SIGKILL'); setInterval(() => {}, 1000);")
    assert run_node("process.stdout.write('again')").stdout == "again"
    assert node_host._host.proc.pid != crashed.proc.pid
    pipes = crashed.proc.stdin, crashed.proc.stdout, crashed.proc.stderr
    assert all(pipe.closed for pipe in pipes)


def test_the_host_exits_when_its_test_process_goes_away_even_mid_case(host):
    if node_host.NODE is None:
        pytest.skip("node is not installed")
    orphan = node_host._Host()
    try:
        request = {"id": 1, "script": "while (true) {}", "argv": [], "timeoutMs": None,
                   "env": None, "cwd": None}
        orphan.proc.stdin.write(json.dumps(request).encode("ascii") + b"\n")
        orphan.proc.stdin.flush()
        # A killed test process closes its end of the pipe; nothing else stops the host.
        orphan.proc.stdin.close()
        assert orphan.proc.wait(timeout=10) == 0
    finally:
        orphan.stop()
    assert orphan.proc.stdout.closed and orphan.proc.stderr.closed


def test_a_missing_node_skips_instead_of_failing(transport, monkeypatch):
    monkeypatch.setattr(node_host, "NODE", None)
    with pytest.raises(pytest.skip.Exception, match="node is not installed"):
        run_node("")
