"""The release smoke check needs only the bundled HEVC decoder, not an encoder."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nd2wsi import smoke


def test_sdpc_smoke_uses_frozen_bytes_and_decoder_only(tmp_path, monkeypatch, capsys):
    av = pytest.importorskip("av")
    open_media, codec, codec_context = av.open, av.Codec, av.CodecContext
    decoders = []

    def open_for_reading(file, mode="r", *args, **kwargs):
        assert mode == "r", "the packaged SDPC smoke test must never encode HEVC"
        return open_media(file, mode, *args, **kwargs)

    def decoder_codec(name, mode="r"):
        assert mode == "r", "the packaged SDPC smoke test must never look up an encoder"
        return codec(name, mode)

    class DecoderContexts:
        @staticmethod
        def create(name, mode=None, *args, **kwargs):
            context = codec_context.create(name, mode, *args, **kwargs)
            assert context.is_decoder and not context.is_encoder, (
                "the packaged SDPC smoke test must never encode HEVC"
            )
            decoders.append(context.name)
            return context

    monkeypatch.setattr(av, "open", open_for_reading)
    monkeypatch.setattr(av, "Codec", decoder_codec)
    monkeypatch.setattr(av, "CodecContext", DecoderContexts)
    smoke._smoke_sdpc(tmp_path)
    assert decoders and set(decoders) == {"hevc"}, "the smoke check decoded no SDPC tile"
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
