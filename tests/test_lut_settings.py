"""Durable per-source LUT preferences without source or Agent/user leakage."""

import copy
import json
import os
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest
from http_fixture import serving

from nd2wsi import lut_settings
from nd2wsi.lut_settings import FORMAT, MAX_BYTES, LutSettingsStore, LutSettingsUnavailable
from nd2wsi.server import SlideRegistry, ViewerState, create_server
from nd2wsi.window_sessions import create_window_session


def attrs(source):
    return {
        "nd2wsi": {"source": source.name, "dtype": "uint16", "rgb": False,
                   "levels": [{"path": "0", "width": 200, "height": 100}],
                   "selection": {"t": 0, "p": 0, "z": "mid"}},
        "omero": {"channels": [
            {"label": label, "color": "FFFFFF", "window": {"min": 0, "max": 65535, "start": 0, "end": 65535}}
            for label in ("DAPI", "CD31")
        ]},
    }


def settings(lo=10.25):
    return {"format": FORMAT, "luts": [
        {"lo": lo, "hi": 4123.75, "gamma": 0.37},
        {"lo": 0, "hi": 65535, "gamma": 1},
    ]}


@pytest.fixture(autouse=True)
def isolated_preferences(tmp_path, monkeypatch):
    monkeypatch.setenv("ND2WSI_LUT_SETTINGS_ROOT", str(tmp_path / "preferences"))


@pytest.fixture
def source(tmp_path):
    source = tmp_path / "research" / "sample.nd2"
    source.parent.mkdir()
    source.write_bytes(b"preserve these original pixels")
    return source


@pytest.fixture
def session(tmp_path, request):
    def create(role):
        value = create_window_session(role, tmp_path / "sessions")
        request.addfinalizer(value.mark_closed)
        return value
    return create


def registered_store(source, *, metadata=None, session=None):
    registry = SlideRegistry(window_session=session)
    st = ViewerState({}, attrs(source) if metadata is None else metadata, source_path=source)
    registry.slides[registry.sid_for(source)] = st
    return registry, st, registry.lut_settings_for(st)


def test_effective_luts_round_trip_across_new_registries_and_user_sessions(source, session):
    metadata = attrs(source)
    before = copy.deepcopy(metadata)
    registry, st, first = registered_store(source, metadata=metadata, session=session("user"))
    assert first.read() == {"ok": True, "available": True, "settings": None}
    assert not first.path.parent.exists()  # first read creates no preference/source directories
    assert first.write(settings()) == {"ok": True}
    assert registry.lut_settings_for(st) is first
    _, _, reopened = registered_store(source, session=session("user"))
    assert reopened.read()["settings"] == settings()
    assert reopened.path == first.path
    assert metadata == before
    assert source.read_bytes() == b"preserve these original pixels"
    assert list(source.parent.iterdir()) == [source]


def test_same_basename_different_directories_and_selections_are_independent(source, tmp_path):
    other = tmp_path / "other" / source.name
    other.parent.mkdir()
    other.write_bytes(source.read_bytes())
    _, _, a = registered_store(source)
    _, _, b = registered_store(other)
    a.write(settings(12))
    assert b.path != a.path and b.read()["settings"] is None
    b.write(settings(34))
    changed = attrs(source)
    changed["nd2wsi"]["selection"]["t"] = 1
    _, _, c = registered_store(source, metadata=changed)
    assert c.path not in (a.path, b.path) and c.read()["settings"] is None
    assert a.read()["settings"] == settings(12)
    assert b.read()["settings"] == settings(34)


@pytest.mark.parametrize("change", ["dtype", "dimensions", "labels", "window", "order", "rgb"])
def test_changed_channel_schema_never_restores_old_settings(source, change):
    _, _, original = registered_store(source)
    original.write(settings())
    saved = original.path.read_bytes()
    metadata = attrs(source)
    if change == "dtype":
        metadata["nd2wsi"]["dtype"] = "float32"
    elif change == "dimensions":
        metadata["nd2wsi"]["levels"][0]["width"] += 1
    elif change == "labels":
        metadata["omero"]["channels"][0]["label"] = "FITC"
    elif change == "window":
        metadata["omero"]["channels"][0]["window"]["end"] = 4095
    elif change == "order":
        metadata["omero"]["channels"].reverse()
    else:
        metadata["nd2wsi"]["rgb"] = True
    _, _, reopened = registered_store(source, metadata=metadata)
    result = reopened.read()
    assert result["available"] is True and result["settings"] is None and result["warning"]
    assert original.path.read_bytes() == saved


def test_replaced_source_rejects_live_save_and_fresh_open_ignores_old_preferences(source):
    _, _, original = registered_store(source)
    original.write(settings())
    saved = original.path.read_bytes()
    old_stat = source.stat()
    source.write_bytes(b"different original pixel bytes"[:old_stat.st_size].ljust(old_stat.st_size, b"!"))
    os.utime(source, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    with pytest.raises(LutSettingsUnavailable):
        original.write(settings(20))
    assert original.read()["available"] is False
    _, _, reopened = registered_store(source)
    assert reopened.read()["settings"] is None
    assert original.path.read_bytes() == saved


def test_windows_change_time_rejects_rewrite_with_identical_python_stat_without_rehash(source, monkeypatch):
    from nd2wsi import windows_fs

    original_stat = source.stat()
    real_stat = Path.stat
    real_fingerprint = lut_settings.quick_fingerprint
    fingerprints = []
    change_time = 133000000000000001

    def shadow_stat(path, *args, **kwargs):
        # Windows creation time, size and restored mtime can all remain equal.
        return original_stat if path == source else real_stat(path, *args, **kwargs)

    def read_change_time(path, expected):
        assert path == source and expected == original_stat
        return change_time

    def fingerprint(path):
        fingerprints.append(path)
        return real_fingerprint(path)

    monkeypatch.setattr(lut_settings.sys, "platform", "win32")
    monkeypatch.setattr(Path, "stat", shadow_stat)
    monkeypatch.setattr(windows_fs, "file_change_time", read_change_time)
    monkeypatch.setattr(lut_settings, "quick_fingerprint", fingerprint)
    _, _, store = registered_store(source)
    store.write(settings(10))
    store.write(settings(20))
    assert store.read()["settings"] == settings(20)
    previous = store.path.read_bytes()
    source.write_bytes(b"x" * original_stat.st_size)
    os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    change_time += 1
    assert source.stat() == original_stat
    with pytest.raises(LutSettingsUnavailable):
        store.write(settings(30))
    assert store.read()["available"] is False
    assert store.path.read_bytes() == previous
    assert fingerprints == [source]


def test_windows_unavailable_change_stamp_fails_closed_without_overwriting(source, monkeypatch):
    from nd2wsi import windows_fs

    monkeypatch.setattr(lut_settings.sys, "platform", "win32")
    monkeypatch.setattr(windows_fs, "file_change_time", lambda *args: 133000000000000001)
    _, _, store = registered_store(source)
    store.write(settings())
    previous = store.path.read_bytes()

    def unavailable(*args):
        raise OSError("FileBasicInfo is unsupported")

    monkeypatch.setattr(windows_fs, "file_change_time", unavailable)
    with pytest.raises(LutSettingsUnavailable):
        store.write(settings(30))
    assert store.read()["available"] is False
    assert store.path.read_bytes() == previous
    _, _, reopened = registered_store(source)
    assert reopened.read()["available"] is False


def test_windows_unsupported_filesystem_keeps_persistence_and_detects_restored_mtime(source, monkeypatch):
    from nd2wsi import windows_fs

    captured = source.stat()
    real_stat, real_fstat = Path.stat, os.fstat
    guards = set()

    class WindowsStat:
        def __init__(self, *, by_handle=False):
            self.st_birthtime_ns = captured.st_ctime_ns
            self.st_ctime_ns = captured.st_ctime_ns + (100 if by_handle else 0)

        def __getattr__(self, name):
            return getattr(captured, name)

    path_stat, handle_stat = WindowsStat(), WindowsStat(by_handle=True)

    def open_guard(path):
        assert path == source
        fd = os.open(path, os.O_RDONLY)
        guards.add(fd)
        return fd

    def fstat(fd):
        return handle_stat if fd in guards else real_fstat(fd)

    # Windows path ctime can be creation time while handle ctime is ChangeTime.
    # Keep their birthtime/restored mtime equal while sampled bytes change.
    # Only source descriptors are shadowed.
    real_close = os.close

    def close(fd):
        guards.discard(fd)
        return real_close(fd)

    monkeypatch.setattr(lut_settings.sys, "platform", "win32")
    monkeypatch.setattr(Path, "stat", lambda path, *a, **kw: path_stat if path == source else real_stat(path, *a, **kw))
    monkeypatch.setattr(windows_fs, "file_change_time", lambda *args: None)
    monkeypatch.setattr(windows_fs, "open_guard", open_guard)
    monkeypatch.setattr(os, "fstat", fstat)
    monkeypatch.setattr(os, "close", close)
    _, _, store = registered_store(source)
    assert store.identity is not None, store._warning
    store.write(settings(20))
    assert store.read()["settings"] == settings(20)
    _, _, reopened = registered_store(source)
    assert reopened.read()["settings"] == settings(20)
    saved = store.path.read_bytes()
    source.write_bytes(b"x" * captured.st_size)
    os.utime(source, ns=(captured.st_atime_ns, captured.st_mtime_ns))
    with pytest.raises(LutSettingsUnavailable):
        store.write(settings(30))
    assert store.read()["available"] is False and store.path.read_bytes() == saved
    assert not guards


@pytest.mark.parametrize("value", [None, [], {}, True, "12", float("nan"), float("inf"), 10**400])
def test_invalid_lut_numbers_are_rejected_without_touching_saved_settings(source, value):
    _, _, store = registered_store(source)
    store.write(settings())
    previous = store.path.read_bytes()
    attempted = settings()
    attempted["luts"][0]["lo"] = value
    with pytest.raises(ValueError):
        store.write(attempted)
    assert store.path.read_bytes() == previous


@pytest.mark.parametrize("mutation", ["format", "count", "null", "extra", "reverse", "gamma_low", "gamma_high", "span", "path"])
def test_invalid_shape_and_ranges_never_save(source, mutation):
    _, _, store = registered_store(source)
    attempted = settings()
    if mutation == "format":
        attempted["format"] = "nd2wsi-lut-settings/2"
    elif mutation == "count":
        attempted["luts"].pop()
    elif mutation == "null":
        attempted["luts"][0] = None
    elif mutation == "extra":
        attempted["luts"][0]["visible"] = True
    elif mutation == "reverse":
        attempted["luts"][0]["hi"] = attempted["luts"][0]["lo"]
    elif mutation == "gamma_low":
        attempted["luts"][0]["gamma"] = 0.2499
    elif mutation == "gamma_high":
        attempted["luts"][0]["gamma"] = 4.0001
    elif mutation == "span":
        attempted["luts"][0].update(lo=-1e308, hi=1e308)
    else:
        attempted["path"] = str(source.parent / "unregistered.nd2")
    with pytest.raises(ValueError):
        store.write(attempted)
    assert not store.path.parent.exists()


def test_integer_fractional_default_and_float_narrow_window_remain_exact(source):
    metadata = attrs(source)
    metadata["omero"]["channels"][0]["window"].update(start=0.25, end=0.75)
    store = LutSettingsStore(source, metadata)
    exact = settings()
    exact["luts"][0] = {"lo": 0.25, "hi": 0.75, "gamma": 1}
    store.write(exact)
    assert store.read()["settings"] == exact
    other = copy.deepcopy(exact)
    other["luts"][0]["gamma"] = 1.1
    with pytest.raises(ValueError, match="Integer"):
        store.write(other)
    metadata["nd2wsi"]["dtype"] = "float32"
    floating = LutSettingsStore(source, metadata)
    other["luts"][0] = {"lo": 0.2000000000001, "hi": 0.2000000000002, "gamma": 0.25}
    floating.write(other)
    assert floating.read()["settings"] == other


@pytest.mark.parametrize("damage", ["json", "version", "too_large", "nan", "nesting"])
def test_invalid_saved_record_falls_back_without_deleting_or_rewriting(source, damage):
    _, _, store = registered_store(source)
    store.write(settings())
    record = json.loads(store.path.read_bytes())
    if damage == "json":
        raw = b"{invalid"
    elif damage == "too_large":
        raw = b" " * (MAX_BYTES + 1)
    elif damage == "nesting":
        raw = b"[" * 2000 + b"0" + b"]" * 2000
    else:
        if damage == "version":
            record["format"] = "unsupported/9"
        else:
            record["settings"]["luts"][0]["gamma"] = float("nan")
        raw = json.dumps(record).encode()
    store.path.write_bytes(raw)
    result = store.read()
    assert result["available"] is True and result["settings"] is None and result["warning"]
    assert store.path.read_bytes() == raw


def test_agent_snapshots_and_saves_are_private_and_new_agents_inherit_latest_user(source, session):
    annotation = source.with_suffix(".annotations.json")
    annotation.write_bytes(b'{"items":[{"id":"manual ROI","points":[[1.25,2]]}]}')
    source_before, annotation_before = source.read_bytes(), annotation.read_bytes()
    _, _, user = registered_store(source, session=session("user"))
    user.write(settings(10))
    user_files = {path.name: path.read_bytes() for path in user.path.parent.iterdir()}
    agent_session = session("agent")
    _, _, agent = registered_store(source, session=agent_session)
    assert agent.read()["settings"] == settings(10)
    assert agent.path.is_relative_to(agent_session.root)
    agent.write(settings(20))
    assert {path.name: path.read_bytes() for path in user.path.parent.iterdir()} == user_files
    user.write(settings(30))
    assert agent.read()["settings"] == settings(20)
    _, _, reopened = registered_store(source, session=agent_session)
    assert reopened.read()["settings"] == settings(20)
    _, _, another = registered_store(source, session=session("agent"))
    assert another.read()["settings"] == settings(30)
    assert another.path != agent.path != user.path
    assert source.read_bytes() == source_before and annotation.read_bytes() == annotation_before


@pytest.mark.parametrize("invalid_shared", [False, True])
def test_agent_remembers_absent_or_invalid_snapshot_without_copying_it(source, session, invalid_shared):
    _, _, user = registered_store(source)
    if invalid_shared:
        user.path.parent.mkdir(parents=True)
        user.path.write_bytes(b"invalid original settings")
    agent_session = session("agent")
    _, _, agent = registered_store(source, session=agent_session)
    assert agent.read()["settings"] is None
    assert not agent.path.exists()
    if invalid_shared:
        assert user.path.read_bytes() == b"invalid original settings"
    user.write(settings(40))
    _, _, reopened = registered_store(source, session=agent_session)
    assert reopened.read()["settings"] is None
    reopened.write(settings(50))
    assert user.read()["settings"] == settings(40)


def test_missing_source_disables_persistence_without_any_writes(source):
    _, _, store = registered_store(source)
    source.unlink()
    assert store.read()["available"] is False
    with pytest.raises(LutSettingsUnavailable):
        store.write(settings())
    assert not store.path.parent.exists()
    reopened = LutSettingsStore(source, attrs(source))
    assert reopened.read()["available"] is False
    with pytest.raises(LutSettingsUnavailable):
        reopened.write(settings())
    assert not lut_settings.settings_root().exists()


def test_failed_atomic_replace_preserves_previous_record(source, monkeypatch):
    _, _, store = registered_store(source)
    store.write(settings())
    previous = store.path.read_bytes()
    def fail(*args):
        raise OSError("simulated replace failure")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="simulated"):
        store.write(settings(90))
    assert store.path.read_bytes() == previous
    assert not list(store.path.parent.glob("*.tmp"))


def test_user_last_successful_write_wins_and_source_hash_is_not_repeated(source, monkeypatch):
    calls = []
    fingerprint = lut_settings.quick_fingerprint
    def counted(path):
        calls.append(path)
        return fingerprint(path)
    monkeypatch.setattr(lut_settings, "quick_fingerprint", counted)
    _, _, first = registered_store(source)
    first.read()
    first.read()
    first.write(settings(10))
    first.write(settings(20))
    assert calls == [source.resolve()]
    _, _, second = registered_store(source)
    second.write(settings(30))
    assert first.read()["settings"] == settings(30)


@contextmanager
def http_server(source):
    server = create_server([], port=0)
    sid = server.registry.sid_for(source)
    server.registry.slides[sid] = ViewerState({}, attrs(source), source_path=source)
    url = f"http://127.0.0.1:{server.server_address[1]}/{server.token}/s/{sid}/api/lut-settings"
    with serving(server):
        yield server, url


def request(url, payload=None, *, raw=None, headers=None):
    data = raw if raw is not None else (None if payload is None else json.dumps(payload).encode())
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def test_http_contract_capability_gate_validation_size_and_stale_source(source):
    with http_server(source) as (server, url):
        assert request(url) == (200, {"ok": True, "available": True, "settings": None})
        assert request(url, settings()) == (200, {"ok": True})
        assert request(url)[1]["settings"] == settings()
        assert request(url.replace(server.token, "not-authorized"), settings())[0] == 404
        assert request(url, settings(), headers={"Origin": "https://untrusted.example"})[0] == 404
        for raw in (b"{bad", b"null", b"[]", b" " * (MAX_BYTES + 1), b"[" * 2000 + b"0" + b"]" * 2000):
            assert request(url, raw=raw)[0] == 400
        assert request(url, {**settings(), "path": str(source)})[0] == 400
        source.unlink()
        assert request(url)[1]["available"] is False
        assert request(url, settings())[0] == 409


@pytest.mark.parametrize("platform", ["darwin", "win32", "linux"])
def test_platform_storage_roots_are_independent_of_source_folder(tmp_path, monkeypatch, platform):
    monkeypatch.delenv("ND2WSI_LUT_SETTINGS_ROOT")
    monkeypatch.setattr(lut_settings.sys, "platform", platform)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "windows-data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "linux-state"))
    base = {"darwin": tmp_path / "home" / "Library" / "Application Support",
            "win32": tmp_path / "windows-data", "linux": tmp_path / "linux-state"}[platform]
    assert lut_settings.settings_root() == base / "nd2wsi-viewer" / "lut-settings"
