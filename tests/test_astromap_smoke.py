"""Exercise the small release smoke fixture without a full native app build."""

from unittest.mock import Mock

from nd2wsi import smoke


def test_packaged_astromap_smoke_checks_real_jpeg_txt_and_pdf(tmp_path, capsys):
    smoke._smoke_astromap(tmp_path)
    assert "smoke ok: Astromap JPEG, TXT and PDF" in capsys.readouterr().out


def test_release_smoke_fails_before_opening_nd2_when_astromap_is_unavailable(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(smoke, "_smoke_sdpc", lambda work: None)
    required = Mock(side_effect=RuntimeError("PDF runtime unavailable"))
    monkeypatch.setattr(smoke, "_smoke_astromap", required)
    assert smoke.run(tmp_path / "never-opened.nd2") == 3
    required.assert_called_once()
    assert "PDF runtime unavailable" in capsys.readouterr().err
