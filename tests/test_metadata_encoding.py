"""UTF-8 sidecars must not depend on the machine's Windows code page."""

import json
from pathlib import Path

from nd2wsi.cache import MANIFEST_NAME, newer_cache_format, read_manifest, write_manifest
from nd2wsi.convert import is_nd2wsi_store
from nd2wsi.plate import PLATE_FORMAT, PlateStore


def test_unicode_metadata_survives_a_legacy_windows_default_encoding(tmp_path, monkeypatch):
    real_open = Path.open

    def legacy_open(self, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        if "b" not in mode and encoding is None:
            encoding = "cp1252"
        return real_open(self, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(Path, "open", legacy_open)
    container = tmp_path / "한글 cache"
    container.mkdir()
    source_name = "원본 세포.nd2"
    payload = {"format": "nd2wsi-cache/99", "complete": True, "source": {"name": source_name}}
    (container / MANIFEST_NAME).write_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    assert read_manifest(container)["source"]["name"] == source_name
    assert newer_cache_format(container) == "nd2wsi-cache/99"

    payload["format"] = PLATE_FORMAT
    (container / MANIFEST_NAME).write_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    assert PlateStore._read(container)["source"]["name"] == source_name
    (container / ".zattrs").write_bytes(json.dumps({"name": source_name, "nd2wsi": {}}, ensure_ascii=False).encode("utf-8"))
    assert is_nd2wsi_store(container)


def test_malformed_utf8_metadata_is_rejected_without_a_locale_exception(tmp_path):
    (tmp_path / MANIFEST_NAME).write_bytes(b'\xff\xfe{"complete": true}')
    (tmp_path / ".zattrs").write_bytes(b'\xff\xfe"nd2wsi"')
    assert read_manifest(tmp_path) is None
    assert newer_cache_format(tmp_path) is None
    assert PlateStore._read(tmp_path) is None
    assert not is_nd2wsi_store(tmp_path)


def test_new_manifests_store_portable_relative_paths(tmp_path):
    slide = tmp_path / "원본.nd2"
    slide.write_bytes(b"source")
    container = tmp_path / "caches" / "slide.nd2wsi-cache"
    container.mkdir(parents=True)
    write_manifest(container, slide, {"name": slide.name}, {}, None, 512, (1, 2, 2), "uint16")
    relative = read_manifest(container)["source"]["relative_path"]
    assert relative == "../../원본.nd2"
    assert (container / relative).resolve() == slide
