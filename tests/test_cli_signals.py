"""CLI output pipes must not change the embedded HTTP server's signal handling."""

import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from nd2wsi import cli, sdpc, server

REPO = Path(__file__).resolve().parents[1]
requires_sigpipe = pytest.mark.skipif(
    not hasattr(signal, "SIGPIPE"), reason="SIGPIPE is a POSIX signal"
)


def run_child(source, *args):
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source), *map(str, args)],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=30,
    )


@requires_sigpipe
@pytest.mark.parametrize("arguments,code", [
    (["--help"], 0),
    (["unknown-command"], 2),
    (["view", "synthetic.sdpc", "--z=mid"], 2),
])
def test_parser_exits_preserve_sigpipe(arguments, code):
    previous = signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    try:
        with pytest.raises(SystemExit) as caught:
            cli.main(arguments)
        assert caught.value.code == code
        assert signal.getsignal(signal.SIGPIPE) == signal.SIG_IGN
    finally:
        signal.signal(signal.SIGPIPE, previous)


@requires_sigpipe
@pytest.mark.parametrize("command", ["view", "serve", "info"])
def test_cli_routes_preserve_sigpipe_while_running(command, monkeypatch):
    observed = []

    def backend(*args, **kwargs):
        observed.append(signal.getsignal(signal.SIGPIPE))
        return {"width": 80, "height": 64}

    monkeypatch.setattr(server, "serve", backend)
    monkeypatch.setattr(sdpc, "sdpc_info", backend)
    previous = signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    try:
        assert cli.main([command, "synthetic.sdpc"]) == 0
        assert observed == [signal.SIG_IGN]
        assert signal.getsignal(signal.SIGPIPE) == signal.SIG_IGN
    finally:
        signal.signal(signal.SIGPIPE, previous)


@pytest.mark.parametrize("code", [0, 1, 2])
def test_main_preserves_command_return_code(code, monkeypatch):
    monkeypatch.setattr(cli, "_main", lambda argv: code)
    assert cli.main([]) == code


@pytest.mark.parametrize("mode", ["buffered", "large", "help"])
def test_closed_stdout_exits_quietly_and_closes_temporary_fd(mode):
    result = run_child(r"""
        import errno
        import os
        import sys
        from unittest.mock import patch
        from nd2wsi import cli, sdpc

        mode = sys.argv[1]
        metadata = {"description": "x" * (65536 if mode == "large" else 1)}
        reader, writer = os.pipe()
        os.close(reader)
        os.dup2(writer, sys.stdout.fileno())
        os.close(writer)
        temporary_fds = []
        real_open = os.open

        def record_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            temporary_fds.append(fd)
            return fd

        arguments = ["--help"] if mode == "help" else ["info", "synthetic.sdpc", "--json"]
        with patch.object(sdpc, "sdpc_info", return_value=metadata):
            with patch.object(cli.os, "open", side_effect=record_open):
                code = cli.main(arguments)
        assert code == 1
        assert len(temporary_fds) == 1
        try:
            os.fstat(temporary_fds[0])
        except OSError as exc:
            assert exc.errno == errno.EBADF
        else:
            raise AssertionError("the temporary /dev/null descriptor leaked")
        sys.exit(code)
    """, mode)
    assert result.returncode == 1, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""


@requires_sigpipe
def test_disconnected_http_client_survives_after_cli_view():
    result = run_child(r"""
        import signal
        import socket
        from unittest.mock import patch
        from nd2wsi import cli, server

        signal.signal(signal.SIGPIPE, signal.SIG_IGN)
        with patch.object(server, "serve"):
            assert cli.main(["view", "synthetic.sdpc"]) == 0
        server_socket, peer = socket.socketpair()
        try:
            peer.sendall(
                b"GET /static/app.js HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n"
            )
            peer.close()
            server.make_handler(None)(server_socket, ("127.0.0.1", 0), object())
        finally:
            peer.close()
            server_socket.close()
        print("HTTP_CLOSED_PEER_SURVIVED", flush=True)
    """)
    assert result.returncode == 0, result.stderr
    assert "HTTP_CLOSED_PEER_SURVIVED" in result.stdout
    assert result.stderr == ""
