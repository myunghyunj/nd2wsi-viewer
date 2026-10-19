"""The release smoke check needs only the bundled HEVC decoder, not an encoder."""

from pathlib import Path
from types import SimpleNamespace
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


@pytest.mark.parametrize("corruption", [None, "gpl", "missing_restriction"])
def test_frozen_runtime_requires_restricted_lgpl_libraries(corruption):
    flags = (
        "--disable-autodetect --disable-everything --disable-gpl --disable-nonfree "
        "--disable-version3 --disable-static --enable-shared --disable-network "
        "--enable-decoder=hevc --enable-parser=hevc --enable-demuxer=hevc"
    )
    metadata = {name: {"license": "LGPL version 2.1 or later", "configuration": flags}
                for name in ("libavcodec", "libavformat", "libavdevice", "libavfilter",
                             "libavutil", "libswscale", "libswresample")}
    runtime = SimpleNamespace(__version__="16.0.1", _core=SimpleNamespace(library_meta=metadata),
                              codecs_available={"hevc"})
    if corruption == "gpl":
        metadata["libavcodec"]["configuration"] += " --enable-gpl"
    elif corruption == "missing_restriction":
        metadata["libswscale"]["configuration"] = flags.replace("--disable-network", "")
    if corruption:
        with pytest.raises(AssertionError, match="runtime"):
            smoke._verify_sdpc_runtime_license(runtime)
    else:
        smoke._verify_sdpc_runtime_license(runtime)


def test_frozen_smoke_must_check_the_loaded_libraries(tmp_path, monkeypatch):
    pytest.importorskip("av")
    monkeypatch.setattr(smoke.sys, "frozen", True, raising=False)
    check = Mock(side_effect=AssertionError("runtime license rejected"))
    monkeypatch.setattr(smoke, "_verify_sdpc_runtime_license", check)
    with pytest.raises(AssertionError, match="runtime license rejected"):
        smoke._smoke_sdpc(tmp_path)
    check.assert_called_once()
