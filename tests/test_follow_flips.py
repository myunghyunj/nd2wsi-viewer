"""Follow's flips are remembered per file format, and only an Agent's own copy is ever its."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from nd2wsi import follow_flips
from nd2wsi.follow_flips import FORMAT_RECORD, FollowFlipStore, source_format


@pytest.mark.parametrize("meta, source_path, expected", [
    ({"source": "a.nd2"}, None, "nd2"),
    ({"source": "b.nd2svs"}, None, "nd2"),
    ({"source": "c.SVS"}, None, "svs"),
    ({"source": "d.sdpc", "source_format": "sdpc"}, None, "sdpc"),
    ({"source_format": "sdpc"}, None, "sdpc"),
    ({"source": "e.jpg"}, None, "jpeg"),
    ({"source": "e.JPEG"}, None, "jpeg"),
    ({"source": "f.png"}, None, "png"),
    ({"source": "g.tif"}, None, "other"),
    ({}, None, "other"),
    ({}, "x.nd2", "nd2"),
    # The original file name wins over the cache the store was opened from.
    ({"source": "slide.nd2"}, "/data/nd2wsi/caches/slide--t0-p0-zmid.png", "nd2"),
    ({"source": {"not": "a name"}}, None, "other"),
])
def test_source_format_maps_suffixes_and_cache_names(meta, source_path, expected):
    assert source_format({"nd2wsi": meta}, source_path) == expected


def test_source_format_never_fails_on_malformed_attrs():
    assert source_format(None) == "other"
    assert source_format({"nd2wsi": "nd2"}) == "other"
    assert source_format({}, "x.PNG") == "png"


def test_flips_are_remembered_per_format_and_survive_a_new_store(tmp_path):
    first = FollowFlipStore(root=tmp_path)
    assert first.path == tmp_path / "flips.json"
    first.save("nd2", False, True)
    first.save("svs", True, False)
    second = FollowFlipStore(root=tmp_path)
    assert second.load("nd2") == ((False, True), None)
    assert second.load("svs") == ((True, False), None)
    assert second.load("png") == (None, None)
    record = json.loads(second.path.read_text(encoding="utf-8"))
    assert record["format"] == FORMAT_RECORD
    assert set(record["flips"]["nd2"]) == {"flip_x", "flip_y", "changed"}


def test_saving_one_format_keeps_the_others(tmp_path):
    store = FollowFlipStore(root=tmp_path)
    store.save("nd2", True, True)
    store.save("png", False, True)
    store.save("nd2", False, False)
    assert store.load("nd2") == ((False, False), None)
    assert store.load("png") == ((False, True), None)


def test_a_missing_file_restores_nothing_without_a_warning(tmp_path):
    store = FollowFlipStore(root=tmp_path / "never-created")
    assert store.load("nd2") == (None, None)
    assert not (tmp_path / "never-created").exists()  # loading creates nothing


def _write_symlink(path, tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"format": FORMAT_RECORD, "flips": {"nd2": {"flip_x": True, "flip_y": True}}}))
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")


@pytest.mark.parametrize("case", ["garbage", "format", "not-bool", "missing-flip", "oversized", "directory",
                                  "symlink"])
def test_unreadable_files_restore_nothing_and_are_left_untouched(tmp_path, case):
    store = FollowFlipStore(root=tmp_path)
    path = store.path
    if case == "garbage":
        path.write_bytes(b"{not json")
    elif case == "format":
        path.write_text(json.dumps({"format": "nd2wsi-follow-flips/0", "flips": {"nd2": {"flip_x": True,
                                                                                       "flip_y": True}}}))
    elif case == "not-bool":
        path.write_text(json.dumps({"format": FORMAT_RECORD, "flips": {"nd2": {"flip_x": 1, "flip_y": False}}}))
    elif case == "missing-flip":
        path.write_text(json.dumps({"format": FORMAT_RECORD, "flips": {"nd2": {"flip_x": True}}}))
    elif case == "oversized":
        path.write_text(json.dumps({"format": FORMAT_RECORD, "flips": {}, "pad": "x" * follow_flips.MAX_BYTES}))
    elif case == "directory":
        path.mkdir()
    else:
        if not hasattr(os, "O_NOFOLLOW"):
            pytest.skip("O_NOFOLLOW is unavailable")
        _write_symlink(path, tmp_path)
    before = None if path.is_dir() or path.is_symlink() else path.read_bytes()
    stored, warning = store.load("nd2")
    assert stored is None
    assert warning.startswith("Saved Follow flips were not restored")
    if before is not None:
        assert path.read_bytes() == before
    assert path.is_dir() is (case == "directory") and path.is_symlink() is (case == "symlink")


def test_save_replaces_an_unreadable_file(tmp_path):
    store = FollowFlipStore(root=tmp_path)
    store.path.write_bytes(b"\x00garbage")
    store.save("jpeg", True, False)
    assert store.load("jpeg") == ((True, False), None)
    assert json.loads(store.path.read_text(encoding="utf-8"))["flips"].keys() == {"jpeg"}


@pytest.mark.parametrize("error", [PermissionError(13, "Permission denied"),
                                   OSError(32, "The process cannot access the file"), OSError(5, "I/O error")])
def test_save_never_replaces_a_file_it_cannot_read(tmp_path, monkeypatch, error):
    store = FollowFlipStore(root=tmp_path)
    store.save("nd2", False, True)
    store.save("svs", True, False)
    before = store.path.read_bytes()
    real_read, failing = FollowFlipStore._read_record, [True]

    def read_record(self):
        if failing[0]:
            raise error  # for example a scanner holding the file, once
        return real_read(self)

    monkeypatch.setattr(FollowFlipStore, "_read_record", read_record)
    with pytest.raises(OSError):
        store.save("png", True, True)
    failing[0] = False
    assert store.path.read_bytes() == before  # the other formats' flips are kept
    assert store.load("nd2") == ((False, True), None) and store.load("png") == (None, None)
    store.save("png", True, True)  # the next toggle that can read the file saves as usual
    assert store.load("svs") == ((True, False), None) and store.load("png") == ((True, True), None)


def test_save_never_replaces_a_link(tmp_path):
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("O_NOFOLLOW is unavailable")
    store = FollowFlipStore(root=tmp_path)
    _write_symlink(store.path, tmp_path)
    target = (tmp_path / "elsewhere.json").read_bytes()
    with pytest.raises(OSError):
        store.save("png", True, True)
    assert store.path.is_symlink() and (tmp_path / "elsewhere.json").read_bytes() == target


@pytest.mark.parametrize("fmt, flip_x, flip_y", [
    ("tiff", False, False), ("ND2", False, False), (None, False, False), (["nd2"], False, False),
    ("nd2", 1, False), ("nd2", False, None), ("nd2", "true", False),
])
def test_unknown_format_or_non_bool_is_refused(tmp_path, fmt, flip_x, flip_y):
    store = FollowFlipStore(root=tmp_path)
    with pytest.raises(ValueError):
        store.save(fmt, flip_x, flip_y)
    assert not store.path.exists()


def test_agent_window_writes_only_its_own_copy(tmp_path, monkeypatch):
    user_root = tmp_path / "user"
    monkeypatch.setenv("ND2WSI_FOLLOW_FLIPS_ROOT", str(user_root))
    user = FollowFlipStore()
    user.save("nd2", True, True)
    before = user.path.read_bytes()
    agent = FollowFlipStore(SimpleNamespace(role="agent", root=tmp_path / "agent"))
    assert agent.path == tmp_path / "agent" / "stage-follow" / "flips.json"
    assert agent.load("nd2") == (None, None)  # a private copy: the user's flips are not adopted
    agent.save("nd2", False, True)
    assert agent.load("nd2") == ((False, True), None)
    assert user.path.read_bytes() == before
    assert FollowFlipStore(SimpleNamespace(role="user", root=tmp_path / "ignored")).path == user_root / "flips.json"


def test_the_root_override_keeps_tests_out_of_the_users_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("ND2WSI_FOLLOW_FLIPS_ROOT", str(tmp_path / "flips"))
    assert follow_flips.flips_root() == (tmp_path / "flips").resolve()
    monkeypatch.delenv("ND2WSI_FOLLOW_FLIPS_ROOT")
    monkeypatch.setenv("ND2WSI_LUT_SETTINGS_ROOT", str(tmp_path / "luts"))  # the LUT override does not apply
    root = follow_flips.flips_root()
    assert root.parts[-2:] == ("nd2wsi-viewer", "stage-follow") and "luts" not in root.parts
