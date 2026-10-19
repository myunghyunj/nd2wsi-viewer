"""The release smoke check needs only the bundled HEVC decoder, not an encoder."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from nd2wsi import smoke


def test_sdpc_smoke_uses_frozen_bytes_and_decoder_only(tmp_path, monkeypatch, capsys):
    av = pytest.importorskip("av")
    open_media = av.open

    def decode_only(file, mode="r", *args, **kwargs):
        assert mode == "r", "the packaged SDPC smoke test must never encode HEVC"
        return open_media(file, mode, *args, **kwargs)

    monkeypatch.setattr(av, "open", decode_only)
    smoke._smoke_sdpc(tmp_path)
    assert "smoke ok: SDPC" in capsys.readouterr().out


def test_release_smoke_fails_when_required_sdpc_check_fails(tmp_path, monkeypatch, capsys):
    check = Mock(side_effect=RuntimeError("required SDPC decoder unavailable"))
    monkeypatch.setattr(smoke, "_smoke_sdpc", check)
    assert smoke.run(tmp_path / "never-opened.nd2") == 3
    check.assert_called_once()
    assert isinstance(check.call_args.args[0], Path)
    assert "required SDPC decoder unavailable" in capsys.readouterr().err
